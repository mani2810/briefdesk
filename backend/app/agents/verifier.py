"""Citation verification: is every cited sentence actually supported by the source it cites?

Step 1 (free, deterministic): for each (sentence, [n]) pair find the best-matching passage in source n
and score the overlap of content words. Figures are strict: a number in the claim that does not appear in
the source halves the score.
Step 2 (one cheap LLM call, optional): claims that scored weak/unsupported are re-checked together, since
good paraphrases have low word overlap. The LLM can only confirm or reject; it never edits the report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tools.web import split_passages, tokens

CITE = re.compile(r"\[(\d{1,3})\]")
NUMBER = re.compile(r"(?<![A-Za-z])(\d[\d,]*(?:\.\d+)?)\s*(%|percent|crore|lakh|million|billion|bn|mn|k)?", re.I)
VERIFIED, WEAK = 0.5, 0.3


@dataclass
class ClaimResult:
    id: int
    sentence: str
    source_n: int
    score: float
    status: str                       # verified | weak | unsupported | missing_source
    evidence: str = ""


@dataclass
class Verification:
    results: list[ClaimResult] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        checked = [r for r in self.results]
        return round(sum(r.status == "verified" for r in checked) / len(checked), 3) if checked else 1.0

    def counts(self) -> dict:
        out = {"verified": 0, "weak": 0, "unsupported": 0, "missing_source": 0}
        for r in self.results:
            out[r.status] += 1
        return out

    def problems(self) -> list[ClaimResult]:
        return [r for r in self.results if r.status in ("unsupported", "missing_source")]


def cited_sentences(markdown: str) -> list[tuple[str, list[int]]]:
    """(sentence without citation marks, [source numbers]) for every sentence that cites something."""
    body = markdown.split("\n## Sources", 1)[0]
    out = []
    for line in body.splitlines():
        line = line.strip().lstrip("-*>#| ").strip()
        if not line or "[" not in line:
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9₹$])", line):
            nums = [int(n) for n in CITE.findall(sent)]
            if nums:
                clean = re.sub(r"\s*\[\d{1,3}\]", "", sent).strip(" |")
                if len(clean.split()) >= 4:
                    out.append((clean, sorted(set(nums))))
    return out


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUMBER.finditer(text):
        n = m.group(1).replace(",", "").rstrip(".")
        if n and not (len(n) == 4 and n.startswith(("19", "20"))):   # years are context, not the claim
            out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def score_claim(claim: str, source: str) -> tuple[float, str]:
    claim_toks = set(tokens(claim))
    if not claim_toks or not source:
        return 0.0, ""
    best, evidence = 0.0, ""
    for passage in split_passages(source, target_words=60):
        overlap = len(claim_toks & set(tokens(passage))) / len(claim_toks)
        if overlap > best:
            best, evidence = overlap, passage
    claim_nums = _numbers(claim)
    if claim_nums:
        source_nums = _numbers(source)
        if not claim_nums <= source_nums:
            best *= 0.5
    return round(min(best, 1.0), 3), evidence[:600]


def verify(markdown: str, sources: dict[int, str]) -> Verification:
    v = Verification()
    i = 0
    for sentence, nums in cited_sentences(markdown):
        for n in nums:
            i += 1
            if n not in sources:
                v.results.append(ClaimResult(i, sentence, n, 0.0, "missing_source"))
                continue
            score, evidence = score_claim(sentence, sources[n])
            status = "verified" if score >= VERIFIED else "weak" if score >= WEAK else "unsupported"
            v.results.append(ClaimResult(i, sentence, n, score, status, evidence))
    return v


def apply_llm_checks(v: Verification, supported: dict[int, bool]) -> None:
    """LLM second opinion for weak / unsupported claims (it can't touch missing sources)."""
    for r in v.results:
        if r.id in supported and r.status in ("weak", "unsupported"):
            r.status = "verified" if supported[r.id] else "unsupported"
