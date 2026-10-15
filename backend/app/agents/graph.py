"""BriefDesk's multi-agent system: a Supervisor (ReAct: Reason -> Act -> Observe) and five workers.

    START -> supervisor --(conditional edge on state["next"])--> plan_gate | researcher | writer | reviewer | verifier
                ^                                                        |         |         |          |
                +--------------------------------------------------------+---------+---------+----------+
             supervisor --FINISH--> finalize -> END

* The Supervisor reasons over the shared state after EVERY step and picks the next worker (structured
  RouteDecision). A deterministic policy computes the ALLOWED moves and a suggestion; the LLM may deviate
  within ALLOWED (agency), never outside it (safety). If the LLM is unavailable, the policy decides.
* plan_gate pauses the graph (LangGraph interrupt) until the human approves the plan; the checkpointer
  keeps the state, so approval can arrive minutes later or after a restart.
* Steering: messages the user types while the run is going are pulled in at the next supervisor step.
* Revisions re-enter the same thread: the graph resumes with the full state (notes, sources, draft).
* Loops are bounded: per-run LLM-call budget, max supervisor iterations, max research rounds.
"""
from __future__ import annotations

import json
import logging
import operator
import re
from typing import Annotated, Protocol, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..core.config import get_settings
from ..llm.gateway import BudgetExceeded, LLMGateway, LLMUnavailable
from ..tools.registry import Document, ToolRegistry
from ..tools.web import relevant_passages
from . import prompts
from .schemas import ClaimChecks, Findings, Plan, Review, RouteDecision
from .verifier import apply_llm_checks, verify

log = logging.getLogger(__name__)

WORDS = {"quick": 450, "standard": 750, "deep": 1100}
RESEARCH_ROUNDS = {"quick": 2, "standard": 3, "deep": 4}
MAX_REVIEWS, MAX_VERIFY_ROUNDS = 3, 2
RESEARCH_HINT = re.compile(r"\b(add|cover|include|research|more (data|detail|facts|sources)|latest|recent|compare|"
                           r"new section|section on|find|statistics|numbers|figures)\b", re.I)


class RunCancelled(Exception):
    pass


def _merge_sources(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    seen = {s["n"]: s for s in (old or [])}
    for s in new or []:
        seen[s["n"]] = s
    return sorted(seen.values(), key=lambda s: s["n"])


class BriefState(TypedDict, total=False):
    brief_id: int
    topic: str
    audience: str
    depth: str
    auto_approve: bool
    title: str
    plan: list[str]
    plan_approved: bool
    notes: Annotated[list[str], operator.add]
    sources: Annotated[list[dict], _merge_sources]       # [{n, title, url, origin}]
    draft: str
    gap: str | None
    review: dict | None
    reviews: int
    research_rounds: int
    verification: dict | None
    verify_rounds: int
    last_worker: str | None
    next: str
    iterations: int
    steer: list[str]                                      # guidance not yet acted on
    revision: str | None
    report: str
    version: int


class Deps(Protocol):
    """What the graph needs from the outside world (DB, chat stream). Implemented by services.runner."""
    gateway: LLMGateway
    tools: ToolRegistry

    def emit(self, brief_id: int, kind: str, payload: dict, agent: str | None = None) -> None: ...
    def delta(self, brief_id: int, agent: str, text: str) -> None: ...
    def set_status(self, brief_id: int, status: str) -> None: ...
    def check_cancel(self, brief_id: int) -> None: ...
    def pull_steering(self, brief_id: int) -> list[str]: ...
    def add_sources(self, brief_id: int, docs: list[Document]) -> list[dict]: ...
    def source_texts(self, brief_id: int) -> dict[int, str]: ...
    def save_report(self, brief_id: int, title: str, markdown: str, verification: dict | None,
                    instruction: str | None) -> int: ...


# ---------------------------------------------------------------- policy (deterministic)
def can_research(s: BriefState) -> bool:
    limit = RESEARCH_ROUNDS.get(s.get("depth", "standard"), 3) + (1 if s.get("revision") else 0)
    return s.get("research_rounds", 0) < limit


def allowed_moves(s: BriefState) -> list[str]:
    """The workflow as a state machine. The Supervisor LLM only chooses when there is a real choice."""
    research = ["researcher"] if can_research(s) else []
    lw = s.get("last_worker")
    review = s.get("review") or {}
    if not s.get("notes"):
        return ["researcher"]
    if s.get("steer") or lw in ("researcher", "revision") or (lw == "writer" and s.get("gap")):
        return research + ["writer"]
    if not s.get("draft"):
        return research + ["writer"]
    if lw == "writer":
        return ["verifier"] if s.get("reviews", 0) >= MAX_REVIEWS else ["reviewer"]
    if lw == "reviewer":
        verdict = review.get("verdict")
        if verdict == "research":
            return research + ["writer"]
        if verdict == "rewrite" and s.get("reviews", 0) < MAX_REVIEWS:
            return ["writer"]
        return ["verifier"]
    if lw == "verifier":
        problems = (s.get("verification") or {}).get("problems")
        if problems and s.get("verify_rounds", 0) < MAX_VERIFY_ROUNDS:
            return research + ["writer", "FINISH"]
        return ["FINISH"]
    return ["reviewer"]


def policy(s: BriefState) -> tuple[str, str]:
    """Suggested move (and the decision when the LLM is unavailable or there is only one option)."""
    allowed = allowed_moves(s)
    lw = s.get("last_worker")
    review = s.get("review") or {}

    def pick(move: str, reason: str) -> tuple[str, str]:
        return (move if move in allowed else allowed[-1], reason)

    if s.get("steer"):
        text = " ".join(s["steer"])
        if RESEARCH_HINT.search(text):
            return pick("researcher", f"User asked: '{text[:80]}' - needs new research.")
        return pick("writer", f"User asked: '{text[:80]}' - the writer can apply it.")
    if lw == "revision":
        if RESEARCH_HINT.search(s.get("revision") or ""):
            return pick("researcher", "The revision needs new facts.")
        return pick("writer", "The revision can be made with the existing notes.")
    if not s.get("notes"):
        return "researcher", "No research notes yet."
    if lw == "writer" and s.get("gap"):
        return pick("researcher", f"Writer reported a gap: {s['gap'][:120]}")
    if lw == "researcher":
        thin = sum(len(n.splitlines()) for n in s.get("notes", [])) < 6 and s.get("research_rounds", 0) < 2
        return pick("researcher" if thin else "writer",
                    "The notes are still thin; research more." if thin else "Enough material; draft the report.")
    if not s.get("draft"):
        return pick("writer", "Notes are ready; draft the report.")
    if lw == "writer":
        return pick("reviewer", "A new draft needs review.")
    if lw == "reviewer":
        verdict = review.get("verdict")
        if verdict == "research":
            return pick("researcher", "Reviewer needs more facts: " + "; ".join(review.get("missing", []))[:150])
        if verdict == "rewrite":
            return pick("writer", "Reviewer asked for a rewrite.")
        return pick("verifier", "Reviewer approved; check every citation against its source.")
    if lw == "verifier":
        v = s.get("verification") or {}
        if v.get("problems") and "writer" in allowed:
            return pick("writer", f"{len(v['problems'])} cited sentence(s) are not supported by their source.")
        return pick("FINISH", "Citations checked; publish the report.")
    return pick("reviewer", "Check the current draft.")


def clean_text(text: str) -> str:
    """Models like typographic characters fonts and exports handle badly (non-breaking hyphen, narrow nbsp)."""
    return (text.replace("\u2011", "-").replace("\u2010", "-").replace("\u202f", " ").replace("\u00a0", " ")
            .replace("\u200b", ""))


def _compact(messages: list, keep: int = 3) -> list:
    """ReAct history grows every step and is re-sent every call. Keep the last `keep` tool results in full and
    shrink older ones to their first line - the model still knows what it did, at a fraction of the tokens."""
    tool_idx = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    old = set(tool_idx[:-keep]) if len(tool_idx) > keep else set()
    out = []
    for i, m in enumerate(messages):
        if i in old:
            first = str(m.content).replace("<tool_result>", "").strip().splitlines()[:1]
            out.append(ToolMessage((first[0][:180] if first else "") + " ...(older result, summarised)",
                                   tool_call_id=m.tool_call_id))
        else:
            out.append(m)
    return out


def _sources_list(s: BriefState) -> str:
    return "\n".join(f"[{x['n']}] {x['title']}" for x in s.get("sources", [])) or "none"


# ---------------------------------------------------------------- graph
def build_graph(deps: Deps, checkpointer=None):
    gw, settings = deps.gateway, get_settings()

    def trace(s: BriefState, agent: str, event: str, detail: str, **extra) -> None:
        deps.emit(s["brief_id"], "trace", {"event": event, "detail": detail, **extra}, agent=agent)

    # ------------------------------------------------------------ supervisor
    def supervisor(s: BriefState) -> dict:
        bid = s["brief_id"]
        deps.check_cancel(bid)
        update: dict = {"iterations": s.get("iterations", 0) + 1}

        if not s.get("plan"):
            deps.set_status(bid, "planning")
            try:
                plan = gw.structured("planner", [SystemMessage(prompts.PLANNER), HumanMessage(
                    f"Topic: {s['topic']}\nAudience: {s.get('audience', 'business readers')}\n"
                    f"Depth: {s.get('depth', 'standard')}")], Plan, tier="large")
                questions = [q.strip() for q in plan.sub_questions if q.strip()][:6] or [s["topic"]]
                title = plan.title.strip()[:120] or s["topic"][:120]
            except LLMUnavailable as exc:
                if isinstance(exc, BudgetExceeded) or exc.kind == "config":
                    raise
                questions, title = [s["topic"]], s["topic"][:120]
            update.update(plan=questions, title=title, next="plan_gate")
            trace(s, "supervisor", "plan", f"Plan with {len(questions)} sub-questions", plan=questions, title=title)
            return update

        if not s.get("plan_approved"):
            update["next"] = "plan_gate"
            return update

        steer = list(s.get("steer") or []) + deps.pull_steering(bid)
        if len(steer) > len(s.get("steer") or []):
            trace(s, "supervisor", "steer", "Picked up your guidance: " + " | ".join(steer)[:300])
        state = {**s, "steer": steer}
        update["steer"] = steer

        if update["iterations"] > settings.run_max_iterations:
            nxt = "FINISH" if s.get("draft") else "researcher"
            update["next"] = nxt
            trace(s, "supervisor", "route", f"{nxt} - iteration guard reached", next=nxt)
            return update

        allowed = allowed_moves(state)
        suggestion, why = policy(state)
        review = s.get("review") or {}
        verification = s.get("verification") or {}
        summary = (
            f"Topic: {s['topic']}\nPlan: {json.dumps(s.get('plan'))}\n"
            f"Research rounds: {s.get('research_rounds', 0)}, notes: {len(s.get('notes', []))}, "
            f"sources: {len(s.get('sources', []))}\n"
            f"Draft: {'yes, ' + str(len((s.get('draft') or '').split())) + ' words' if s.get('draft') else 'no'}\n"
            f"Last worker: {s.get('last_worker')}\nWriter gap: {s.get('gap')}\n"
            f"Last review: {review.get('verdict')} - {review.get('feedback', '')[:300]}\n"
            f"Verification: {verification.get('counts')}\n"
            f"User guidance waiting: {steer or 'none'}\nRevision request: {s.get('revision') or 'none'}\n"
            f"ALLOWED: {allowed}\nPolicy suggestion: {suggestion} ({why})")
        thought = ""
        if len(allowed) == 1:                     # no real choice: don't spend an LLM call on it
            nxt, reason = allowed[0], why
        else:
            try:
                d = gw.structured("supervisor", [SystemMessage(prompts.SUPERVISOR), HumanMessage(summary)],
                                  RouteDecision)
                nxt, reason, thought = d.next, d.reason, d.thought
                if nxt not in allowed:
                    nxt, reason = suggestion, f"{why} (policy: '{d.next}' was not allowed)"
            except BudgetExceeded:
                raise
            except LLMUnavailable:
                nxt, reason = suggestion, f"{why} (policy decision)"
        update["next"] = nxt
        trace(s, "supervisor", "route", f"{nxt} - {reason}", next=nxt, thought=thought)
        return update

    # ------------------------------------------------------------ plan approval (human in the loop)
    def plan_gate(s: BriefState) -> dict:
        bid = s["brief_id"]
        if s.get("auto_approve"):
            trace(s, "supervisor", "plan_approved", "Plan auto-approved")
            return {"plan_approved": True}
        deps.set_status(bid, "awaiting_approval")
        answer = interrupt({"plan": s["plan"], "title": s.get("title")})   # pauses here; resumes with the answer
        plan = [q.strip() for q in (answer or {}).get("plan", s["plan"]) if str(q).strip()][:6] or s["plan"]
        title = (answer or {}).get("title") or s.get("title")
        trace(s, "supervisor", "plan_approved", "Plan approved - starting research", plan=plan)
        return {"plan": plan, "title": title, "plan_approved": True}

    # ------------------------------------------------------------ researcher (ReAct with tools)
    def researcher(s: BriefState) -> dict:
        bid = s["brief_id"]
        deps.check_cancel(bid)
        deps.set_status(bid, "researching")
        review = s.get("review") or {}
        focus_parts = []
        if s.get("gap"):
            focus_parts.append(f"Writer needs: {s['gap']}")
        if review.get("verdict") == "research":
            focus_parts.append("Reviewer needs: " + "; ".join(review.get("missing") or [review.get("feedback", "")]))
        if s.get("steer"):
            focus_parts.append("User guidance: " + " | ".join(s["steer"]))
        if s.get("revision"):
            focus_parts.append(f"Revision request: {s['revision']}")
        focus = "\n".join(focus_parts) or "Initial research: cover every sub-question of the plan."
        already = {x["url"] for x in s.get("sources", [])}
        max_steps = settings.research_steps.get(s.get("depth", "standard"), 5)

        messages: list = [SystemMessage(prompts.RESEARCHER), HumanMessage(
            f"Topic: {s['topic']}\nAudience: {s.get('audience')}\nPlan:\n"
            + "\n".join(f"{i}. {q}" for i, q in enumerate(s.get("plan", []), 1))
            + f"\n\nRESEARCH FOCUS:\n{focus}\n\nALREADY READ:\n" + (_sources_list(s) if already else "none"))]
        new_sources: list[dict] = []
        new_docs: dict[int, tuple[dict, str]] = {}
        llm_failed = False
        for _ in range(max_steps):
            deps.check_cancel(bid)
            try:
                ai: AIMessage = gw.tools("researcher", _compact(messages), deps.tools.schemas())
            except BudgetExceeded:
                raise
            except LLMUnavailable as exc:
                trace(s, "researcher", "warning", f"Model unavailable ({exc.reason}); using what was found so far")
                llm_failed = True
                break
            messages.append(ai)
            calls = (ai.tool_calls or [])[:4]
            if not calls:
                break
            for call in calls:
                name, args = call["name"], call.get("args") or {}
                label = args.get("query") or args.get("url") or json.dumps(args)[:120]
                if name == "read_page" and args.get("url") in already:
                    messages.append(ToolMessage("Already read; pick another URL.", tool_call_id=call["id"]))
                    continue
                trace(s, "researcher", "tool", f"{name}: {label}", tool=name, args=args)
                out = deps.tools.call(name, args)
                registered = deps.add_sources(bid, out.documents) if out.documents else []
                text = out.text
                if registered:
                    src = registered[0]
                    already.add(src["url"])
                    new_sources.extend(registered)
                    new_docs[src["n"]] = (src, out.text)
                    text = f"[{src['n']}] {src['title']} ({src['url']})\n{out.text}"
                    trace(s, "researcher", "source", f"[{src['n']}] {src['title']}", n=src["n"], url=src["url"],
                          origin=src.get("origin", "web"))
                messages.append(ToolMessage(f"<tool_result>\n{text[:2600]}\n</tool_result>", tool_call_id=call["id"]))

        if len(new_docs) < 2:
            # The model read too little (or is rate-limited): open the best results ourselves - no LLM needed.
            found = [u for m in messages if isinstance(m, ToolMessage)
                     for u in re.findall(r"URL: (\S+)", str(m.content))]
            queries = [("", found)] + [(q, None) for q in [s["topic"]] + list(s.get("plan", []))[:2]]
            for q, urls in queries:
                if len(new_docs) >= 3:
                    break
                if urls is None:
                    urls = re.findall(r"URL: (\S+)", deps.tools.call("web_search", {"query": q}).text)[:2]
                for url in urls:
                    if url in already or len(new_docs) >= 3:
                        continue
                    out = deps.tools.call("read_page", {"url": url, "focus": q or s["topic"]})
                    for src in (deps.add_sources(bid, out.documents) if out.documents else []):
                        already.add(src["url"])
                        new_sources.append(src)
                        new_docs[src["n"]] = (src, out.text)
                        trace(s, "researcher", "source", f"[{src['n']}] {src['title']}", n=src["n"], url=src["url"])
            if not new_docs and not s.get("notes"):
                raise LLMUnavailable("no sources could be read (search or network unavailable)")

        notes_text = ""
        if new_docs:
            want = focus + " " + " ".join(s.get("plan", []))
            excerpts = "\n\n".join(
                f"<source n={n}>\n{src['title']}\n{relevant_passages(text, want, 260)}\n</source>"
                for n, (src, text) in sorted(new_docs.items()))
            valid = {x["n"] for x in s.get("sources", [])} | set(new_docs)
            try:
                if llm_failed:
                    raise LLMUnavailable("skip")
                f: Findings = gw.structured("notes", [SystemMessage(prompts.NOTES), HumanMessage(
                    f"Topic: {s['topic']}\nPlan:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(s['plan'], 1))
                    + f"\nFocus: {focus}\n\nSOURCE EXCERPTS:\n{excerpts}")], Findings)
                lines = []
                for item in f.findings:
                    cites = [n for n in item.sources if n in valid]
                    if cites and item.claim.strip():
                        lines.append(f"- (Q{item.sub_question}) {item.claim.strip()} "
                                     + "".join(f"[{n}]" for n in sorted(set(cites))))
                notes_text = "\n".join(lines)
                if f.gaps:
                    notes_text += "\nGaps: " + "; ".join(f.gaps[:5])
            except BudgetExceeded:
                raise
            except LLMUnavailable:
                notes_text = "\n".join(f"- {relevant_passages(text, s['topic'], 60)} [{n}]"
                                       for n, (src, text) in sorted(new_docs.items()))
            trace(s, "researcher", "notes", f"{notes_text.count(chr(10) + '-') + 1} findings from "
                                            f"{len(new_docs)} new source(s)")
        return {"notes": [notes_text] if notes_text else [], "sources": new_sources, "gap": None,
                "research_rounds": s.get("research_rounds", 0) + 1, "last_worker": "researcher", "steer": []}

    # ------------------------------------------------------------ writer (streams tokens to the chat)
    def writer(s: BriefState) -> dict:
        bid = s["brief_id"]
        deps.check_cancel(bid)
        deps.set_status(bid, "writing")
        depth = s.get("depth", "standard")
        can_gap = s.get("research_rounds", 0) < RESEARCH_ROUNDS.get(depth, 3) and not s.get("revision")
        review, verification = s.get("review") or {}, s.get("verification") or {}
        instructions = []
        if s.get("revision"):
            instructions.append(f"REVISION REQUEST from the user: {s['revision']}")
        if s.get("steer"):
            instructions.append("USER GUIDANCE: " + " | ".join(s["steer"]))
        if review.get("verdict") in ("rewrite", "research") and review.get("feedback"):
            instructions.append(f"REVIEWER FEEDBACK: {review['feedback']}")
        if verification.get("problems") and s.get("last_worker") == "verifier":
            instructions.append("THESE SENTENCES ARE NOT SUPPORTED BY THEIR CITED SOURCE - remove them, rephrase them "
                                "to match the source, or cite the right source:\n"
                                + "\n".join(f"- {p['sentence']} [{p['n']}]" for p in verification["problems"][:10]))
        findings = sum(1 for n in s.get("notes", []) for line in n.splitlines() if line.startswith("- "))
        words = max(250, min(WORDS.get(depth, 750), 120 + 60 * findings))   # thin notes -> shorter report
        system = prompts.WRITER.format(words=words, insufficient_rule=(
            prompts.INSUFFICIENT_ALLOWED if can_gap else prompts.INSUFFICIENT_FORBIDDEN))
        user = (f"Title: {s.get('title')}\nTopic: {s['topic']}\nAudience: {s.get('audience')}\nPlan:\n"
                + "\n".join(f"{i}. {q}" for i, q in enumerate(s.get('plan', []), 1))
                + f"\n\nAVAILABLE SOURCES:\n{_sources_list(s)}\n\n<notes>\n" + "\n".join(s.get("notes", []))
                + "\n</notes>\n\n" + ("\n".join(instructions) + "\n\n" if instructions else "")
                + (f"<draft>\nCURRENT DRAFT (revise it):\n{s['draft']}\n</draft>" if s.get("draft") else ""))
        deps.emit(bid, "draft_start", {"version": s.get("version", 0) + 1}, agent="writer")
        out = gw.text("writer", [SystemMessage(system), HumanMessage(user)], tier="large",
                      on_token=lambda t: deps.delta(bid, "writer", t), max_tokens=2600 if depth == "deep" else 2000)
        out = clean_text(re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out.strip()))
        if out.upper().startswith("INSUFFICIENT"):
            gap = out.split(":", 1)[-1].strip()[:300] or "more facts"
            trace(s, "writer", "gap", f"Not enough material: {gap}")
            return {"gap": gap, "last_worker": "writer"}
        # Drop the model's own sources list if it added one; the system generates it from real sources.
        out = re.split(r"\n#{1,3}\s*(Sources|References)\b", out, flags=re.I)[0].strip()
        trace(s, "writer", "draft", f"Draft ready ({len(out.split())} words)")
        return {"draft": out, "gap": None, "review": None, "verification": None, "last_worker": "writer",
                "steer": []}

    # ------------------------------------------------------------ reviewer
    def reviewer(s: BriefState) -> dict:
        bid = s["brief_id"]
        deps.check_cancel(bid)
        deps.set_status(bid, "reviewing")
        try:
            r: Review = gw.structured("reviewer", [SystemMessage(prompts.REVIEWER), HumanMessage(
                f"Audience: {s.get('audience')}\nPlan:\n" + "\n".join(f"- {q}" for q in s.get("plan", []))
                + f"\nReview round: {s.get('reviews', 0) + 1} of {MAX_REVIEWS}\n"
                + "More research possible: "
                + ("yes" if can_research(s) else "no - approve if the draft states its gaps honestly")
                + "\n\n<notes>\n"
                + "\n".join(s.get("notes", []))[:9000] + f"\n</notes>\n\n<draft>\n{s['draft']}\n</draft>")],
                Review, tier="large")
            review = r.model_dump()
        except BudgetExceeded:
            raise
        except LLMUnavailable as exc:
            review = {"verdict": "approve", "feedback": f"Automatic approval: reviewer unavailable ({exc.reason}).",
                      "missing": []}
        trace(s, "reviewer", review["verdict"], review.get("feedback") or "Approved", verdict=review["verdict"])
        return {"review": review, "reviews": s.get("reviews", 0) + 1, "last_worker": "reviewer"}

    # ------------------------------------------------------------ verifier (citations vs source text)
    def verifier(s: BriefState) -> dict:
        bid = s["brief_id"]
        deps.check_cancel(bid)
        deps.set_status(bid, "verifying")
        v = verify(s["draft"], deps.source_texts(bid))
        doubtful = [r for r in v.results if r.status in ("weak", "unsupported")][:10]
        if doubtful:
            claims = "\n\n".join(f"id={r.id}\nCLAIM: {r.sentence}\n<source>EVIDENCE from [{r.source_n}]: "
                                 f"{r.evidence or '(no matching passage)'}</source>" for r in doubtful)
            try:
                checks: ClaimChecks = gw.structured("verifier", [SystemMessage(prompts.VERIFIER),
                                                                 HumanMessage(claims)], ClaimChecks)
                apply_llm_checks(v, {c.id: c.supported for c in checks.checks})
            except BudgetExceeded:
                raise
            except LLMUnavailable:
                pass
        counts = v.counts()
        problems = [{"sentence": r.sentence, "n": r.source_n, "status": r.status} for r in v.problems()]
        result = {"ratio": v.ratio, "counts": counts, "problems": problems,
                  "results": [r.__dict__ for r in v.results]}
        trace(s, "verifier", "checked", f"{counts['verified']} verified, {counts['weak']} weak, "
                                        f"{counts['unsupported'] + counts['missing_source']} unsupported "
                                        f"({round(v.ratio * 100)}% verified)", counts=counts)
        return {"verification": result, "verify_rounds": s.get("verify_rounds", 0) + 1, "last_worker": "verifier"}

    # ------------------------------------------------------------ finalize
    def finalize(s: BriefState) -> dict:
        bid = s["brief_id"]
        draft = s.get("draft") or f"# {s.get('title') or s['topic']}\n\n_Not enough material was gathered._"
        verification = s.get("verification")
        if not verification and s.get("draft"):
            # The final draft changed after the last check (or was never checked): verify it now, no LLM.
            v = verify(draft, deps.source_texts(bid))
            verification = {"ratio": v.ratio, "counts": v.counts(), "results": [r.__dict__ for r in v.results],
                            "problems": [{"sentence": r.sentence, "n": r.source_n, "status": r.status}
                                         for r in v.problems()]}
            trace(s, "verifier", "checked", f"Final check: {verification['counts']['verified']} verified, "
                                            f"{verification['counts']['weak']} weak ({round(v.ratio * 100)}% verified)",
                  counts=verification["counts"])
        cited = {int(n) for n in re.findall(r"\[(\d{1,3})\]", draft)}
        refs = [f"{x['n']}. [{x['title']}]({x['url']})" if x["url"].startswith("http") else
                f"{x['n']}. {x['title']}" for x in s.get("sources", []) if x["n"] in cited]
        report = draft + ("\n\n## Sources\n" + "\n".join(refs) if refs else "")
        version = deps.save_report(bid, clean_text(s.get("title") or s["topic"]), report, verification,
                                   s.get("revision"))
        return {"report": report, "version": version, "revision": None, "steer": [], "verification": verification}

    g = StateGraph(BriefState)
    for name, fn in [("supervisor", supervisor), ("plan_gate", plan_gate), ("researcher", researcher),
                     ("writer", writer), ("reviewer", reviewer), ("verifier", verifier), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["next"], {
        "plan_gate": "plan_gate", "researcher": "researcher", "writer": "writer", "reviewer": "reviewer",
        "verifier": "verifier", "FINISH": "finalize"})
    for worker in ("plan_gate", "researcher", "writer", "reviewer", "verifier"):
        g.add_edge(worker, "supervisor")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
