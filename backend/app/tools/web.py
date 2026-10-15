"""Built-in research tools: web search (DuckDuckGo) and a SAFE page fetcher.

The fetcher is the riskiest code in the app: the URL comes from an LLM that has just read
untrusted web pages, so it must never be usable to reach the server's own network (SSRF).

    * http / https only, no credentials in the URL, standard ports only
    * every hostname is resolved and EVERY address must be public (no loopback, private, link-local,
      CGNAT, multicast, reserved) - checked again on each redirect (max 4)
    * response size and time are capped; only HTML / plain text is parsed
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import re
import socket
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from ..core.config import get_settings

log = logging.getLogger(__name__)

ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
USER_AGENT = "Mozilla/5.0 (compatible; BriefDesk research bot; +https://github.com/mani2810/briefdesk)"
_WORD = re.compile(r"[A-Za-z0-9₹$€%][A-Za-z0-9₹$€%.,'-]*")
STOP = set("""a an the and or of to in on for with by from at as is are was were be been it its this that these those
which who what when where how why than then into over about after before more most less such can could will would
should may might not no yes their there they them our your you we he she his her also has have had
do does did""".split())


class FetchError(Exception):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class Page:
    url: str
    title: str
    text: str


# ---------------------------------------------------------------- search
def web_search(query: str, max_results: int | None = None) -> list[SearchHit]:
    s = get_settings()
    n = max_results or s.search_max_results
    if s.search_provider == "fake":
        return fake_search(query, n)
    try:
        from ddgs import DDGS
        raw = DDGS(timeout=10).text(query, max_results=n) or []
    except Exception as exc:  # noqa: BLE001 - rate limits / network: the agent sees an empty result
        log.warning("search failed", extra={"error": type(exc).__name__})
        return []
    hits = [SearchHit(h.get("title", "")[:300], h.get("href", ""), h.get("body", "")[:500]) for h in raw]
    return [h for h in hits if h.url.startswith(("http://", "https://"))]


# ---------------------------------------------------------------- fetch
def check_url(url: str) -> None:
    """Raise FetchError unless the URL points to a public host on a normal web port."""
    try:
        p = urlparse(url)
    except ValueError as exc:
        raise FetchError("invalid URL") from exc
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("only http(s) URLs can be read")
    if p.username or p.password:
        raise FetchError("URLs with credentials are not allowed")
    try:
        port = p.port
    except ValueError as exc:
        raise FetchError("invalid port") from exc
    if port not in ALLOWED_PORTS:
        raise FetchError("non-standard port")
    host = p.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise FetchError("internal hosts are not allowed")
    try:
        infos = socket.getaddrinfo(host, port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError("host not found") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise FetchError("this address is not public")


def fetch_page(url: str) -> Page:
    s = get_settings()
    if s.search_provider == "fake":
        return fake_page(url)
    current = url
    with httpx.Client(timeout=s.fetch_timeout_seconds, follow_redirects=False,
                      headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}) as client:
        for _ in range(5):
            check_url(current)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("redirect without location")
                    current = urljoin(current, location)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in ("text/html", "application/xhtml+xml", "text/plain", ""):
                    raise FetchError(f"unsupported content type {ctype}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > s.fetch_max_bytes:
                        break
                html = body.decode(resp.encoding or "utf-8", errors="replace")
                return _extract(current, html, ctype)
    raise FetchError("too many redirects")


def _extract(url: str, html: str, ctype: str) -> Page:
    if ctype == "text/plain":
        return Page(url, urlparse(url).hostname or url, html[:60000])
    import trafilatura

    text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True) or ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else (urlparse(url).hostname or url)
    if len(text) < 200:
        raise FetchError("no readable article text on this page")
    return Page(url, _unescape(title), text[:60000])


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


# ---------------------------------------------------------------- passage selection (no LLM, no tokens)
def tokens(text: str) -> list[str]:
    return [w.lower().strip(".,'-") for w in _WORD.findall(text) if w.lower() not in STOP and len(w) > 1]


def split_passages(text: str, target_words: int = 70) -> list[str]:
    """Paragraphs, with long ones cut into ~target-sized sentence groups."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        words = para.split()
        if len(words) <= target_words * 1.5:
            out.append(para)
            continue
        sentences, buf = re.split(r"(?<=[.!?])\s+", para), []
        for sent in sentences:
            buf.append(sent)
            if sum(len(b.split()) for b in buf) >= target_words:
                out.append(" ".join(buf))
                buf = []
        if buf:
            out.append(" ".join(buf))
    return out


def relevant_passages(text: str, query: str, max_words: int = 350) -> str:
    """Pick the passages that best match the query (TF-IDF-ish), returned in document order."""
    passages = split_passages(text)
    if not passages:
        return ""
    q = set(tokens(query))
    df = Counter(t for p in passages for t in set(tokens(p)))
    n = len(passages)

    def score(i_p):
        i, p = i_p
        toks = tokens(p)
        if not toks:
            return 0.0
        hit = sum(math.log(1 + n / df[t]) for t in set(toks) & q)
        numbers = 0.3 if re.search(r"\d", p) else 0.0           # facts with figures are worth more
        return hit / math.sqrt(len(toks)) + numbers + (0.2 if i < 3 else 0)

    ranked = sorted(enumerate(passages), key=score, reverse=True)
    chosen, words = [], 0
    for i, p in ranked:
        w = len(p.split())
        if words + w > max_words and chosen:
            continue
        chosen.append(i)
        words += w
        if words >= max_words:
            break
    return "\n".join(passages[i] for i in sorted(chosen))


# ---------------------------------------------------------------- offline fakes (tests / CI / demo without network)
def _seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def fake_search(query: str, n: int) -> list[SearchHit]:
    base = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "topic"
    sites = ["example-research.org", "data.example.gov", "news.example.com", "industry.example.net",
             "journal.example.edu", "blog.example.io"]
    return [SearchHit(f"{query.title()} - {kind}", f"https://{site}/{base}/{i}",
                      f"Overview of {query}: key figures, trends and analysis ({kind}).")
            for i, (site, kind) in enumerate(zip(sites, ["report", "statistics", "news", "market study",
                                                          "paper", "explainer"]), 1)][:n]


def fake_page(url: str) -> Page:
    p = urlparse(url)
    topic = p.path.strip("/").split("/")[0].replace("-", " ") or "the topic"
    k = _seed(url)
    pct, year, amount = 20 + k % 60, 2020 + k % 6, 100 + k % 900
    text = (f"{topic.title()} has grown quickly. In {year}, adoption reached {pct}% of the target market, "
            f"according to {p.hostname}.\n\n"
            f"The market for {topic} was valued at {amount} million dollars in {year}, with analysts expecting "
            f"double-digit growth over the next five years.\n\n"
            f"Key drivers of {topic} include lower costs, government policy and better infrastructure. "
            f"Small businesses are the fastest-growing segment.\n\n"
            f"Risks for {topic} include regulation, security incidents and uneven access in rural areas.")
    return Page(url, f"{topic.title()} ({p.hostname})", text)
