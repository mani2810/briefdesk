"""Scripted offline "brain" (LLM_MODE=fake): deterministic, network-free answers for every agent.

Used by the test-suite, CI and the UI end-to-end tests, and lets anyone click through BriefDesk
without a Groq key. It follows the same contracts (schemas, tool calls, streaming) as the real model,
so the whole pipeline - graph, verifier, publisher, WebSocket - is exercised for real.
"""
from __future__ import annotations

import re
import time

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from .gateway import Provider


def _last_human(messages: list) -> str:
    for m in reversed(messages):
        if getattr(m, "type", "") == "human":
            return str(m.content)
    return ""


def _field(text: str, name: str, default: str = "") -> str:
    m = re.search(rf"^{name}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else default


def _report_title(text: str) -> str:
    m = re.search(r"^# (.+)$", text, re.M)
    return m.group(1).strip() if m else "Research brief"


class FakeProvider(Provider):
    delay = 0.0          # tests can slow it down to exercise "steer while running"

    def model_name(self, tier):
        return f"fake-{tier}"

    # ---------------------------------------------------------------- structured
    def structured(self, tier, kind, messages, schema: type[BaseModel]):
        from ..agents import schemas as S

        time.sleep(self.delay)
        text = _last_human(messages)
        name = schema.__name__
        if name == "Plan":
            topic = _field(text, "Topic", "the topic")
            return S.Plan(title=topic[:80].title(), sub_questions=[
                f"How large is {topic} today and how fast is it growing?",
                f"What is driving {topic}?",
                f"What are the main risks and challenges for {topic}?",
                f"What is the outlook for {topic}?"]), 0
        if name == "RouteDecision":
            m = re.search(r"Policy suggestion: (\w+) \((.*)\)$", text, re.M)
            nxt = m.group(1) if m else "researcher"
            return S.RouteDecision(thought=f"State reviewed; the policy suggestion fits: {nxt}.", next=nxt,
                                   reason=m.group(2) if m else "start"), 0
        if name == "Findings":
            findings = []
            for n, body in re.findall(r"<source n=(\d+)>\n[^\n]*\n(.*?)\n</source>", text, re.S):
                for i, sent in enumerate(re.split(r"(?<=[.!?])\s+", body.strip())[:3]):
                    if len(sent.split()) >= 5:
                        findings.append(S.Finding(claim=sent.strip(), sources=[int(n)], sub_question=i % 4 + 1))
            return S.Findings(findings=findings, gaps=[]), 0
        if name == "Review":
            return S.Review(verdict="approve", feedback="", missing=[]), 0
        if name == "ClaimChecks":
            return S.ClaimChecks(checks=[S.ClaimCheck(id=int(i), supported=True)
                                         for i in re.findall(r"^id=(\d+)", text, re.M)]), 0
        if name == "ChatIntent":
            return self._intent(text), 0
        if name == "RevisedPlan":
            plan = re.findall(r"^\d+\. (.+)$", text, re.M)
            change = _field(text, "Change")
            return S.RevisedPlan(title=_field(text, "Title", "Research brief"), sub_questions=plan + [change][:1],
                                 change_summary=f"Added: {change}"), 0
        if name == "ExportSpec":
            fmt = self._format(text) or "pdf"
            return S.ExportSpec(format=fmt, scope="summary" if "summary" in text.lower() else "full", section="",
                                style=""), 0
        report = text
        title = _report_title(report)
        bullets = [re.sub(r"\s+", " ", b).strip() for b in re.findall(r"^- (.+)$", report, re.M)] or \
            [s for s in re.split(r"(?<=[.!?])\s+", report) if "[" in s][:6]
        if name == "SlideDeck":
            heads = re.findall(r"^## (.+)$", report, re.M) or ["Overview"]
            return S.SlideDeck(title=title, subtitle="Research brief", slides=[
                S.Slide(title=h, bullets=bullets[i * 2:i * 2 + 3] or ["See report"], notes=f"Talk about {h}.")
                for i, h in enumerate(heads[:7])]), 0
        if name == "Infographic":
            nums = re.findall(r"(\d+(?:\.\d+)?)%", report)[:4]
            return S.Infographic(
                title=title, subtitle="Key facts at a glance",
                stats=[S.StatCard(value=f"{n}%", label="share reported") for n in nums[:3]] or
                [S.StatCard(value="4", label="key themes")],
                points=[S.KeyPoint(headline=f"Point {i + 1}", detail=b[:120]) for i, b in enumerate(bullets[:4])],
                chart_title="Reported shares" if len(nums) >= 3 else "",
                chart_labels=[f"Fact {i + 1}" for i in range(len(nums))] if len(nums) >= 3 else [],
                chart_values=[float(n) for n in nums] if len(nums) >= 3 else [], chart_unit="%"), 0
        if name == "DataTables":
            rows = [[b[:150], (re.findall(r"\[(\d+)\]", b) or [""])[0]] for b in bullets[:20]]
            return S.DataTables(tables=[S.Table(name="Key facts", columns=["Fact", "Source"], rows=rows)]), 0
        raise ValueError(f"fake provider has no script for {name}")

    @staticmethod
    def _format(text: str) -> str:
        low = text.lower()
        for words, fmt in [(("powerpoint", "slides", "deck", "pptx", "presentation"), "pptx"),
                           (("word", "docx"), "docx"), (("excel", "xlsx", "spreadsheet"), "xlsx"),
                           (("csv",), "csv"), (("jpg", "jpeg"), "jpg"),
                           (("image", "png", "infographic", "picture", "poster"), "png"),
                           (("pdf",), "pdf"), (("markdown", " md"), "md"), (("html", "web page"), "html"),
                           (("json",), "json"), (("text file", "txt"), "txt")]:
            if any(w in low for w in words):
                return fmt
        return ""

    def _intent(self, text: str):
        from ..agents import schemas as S

        msg = text.split("USER MESSAGE:", 1)[-1].strip()
        low, state = msg.lower(), _field(text, "Status", "new")
        fmt = self._format(msg)

        def it(intent, fmt=""):
            return S.ChatIntent(intent=intent, instruction=msg, format=fmt, scope="full", section="")
        if any(w in low for w in ("cancel", "stop")):
            return it("cancel")
        if fmt and any(w in low for w in ("give", "export", "download", "as ", "send", "make", "convert", "create")):
            return it("export", fmt)
        if state == "awaiting_approval":
            return it("approve_plan") if any(w in low for w in ("ok", "yes", "go", "good", "approve", "start")) \
                else it("edit_plan")
        if state in ("new", "failed") and not _field(text, "Report exists").startswith("yes"):
            return it("retry") if state == "failed" and "retry" in low else it("start")
        if state in ("planning", "researching", "writing", "reviewing", "verifying"):
            return it("question") if low.endswith("?") else it("steer")
        if low.endswith("?"):
            return it("question")
        if any(w in low for w in ("hi", "hello", "thanks", "thank you")) and len(low) < 25:
            return it("smalltalk")
        return it("revise")

    # ---------------------------------------------------------------- text (writer / answers / summary)
    def text(self, tier, kind, messages, on_token, max_tokens):
        text = _last_human(messages)
        if kind == "writer":
            out = self._report(text)
        elif kind == "answer":
            cites = re.findall(r"\[(\d+)\]", text)[:2]
            out = "Based on the report: " + " ".join(
                f"the sources cover this [{c}]." for c in cites) if cites else "The report does not cover that."
        elif kind == "summary":
            title = _report_title(text)
            pts = re.findall(r"^- (.+)$", text, re.M)[:4]
            out = f"# {title} - summary\n\nA short overview of the brief.\n\n" + "\n".join(f"- {p}" for p in pts)
        else:
            out = "OK."
        if on_token:
            for i in range(0, len(out), 24):
                time.sleep(self.delay / 20)
                on_token(out[i:i + 24])
        return out, 0

    @staticmethod
    def _report(text: str) -> str:
        title = _field(text, "Title", "Research brief")
        notes = re.findall(r"^- \(Q(\d+)\) (.+)$", text, re.M)
        plan = re.findall(r"^\d+\. (.+)$", text, re.M) or ["Overview"]
        rev = re.search(r"REVISION REQUEST from the user: (.+)", text)
        sections = []
        for qi, question in enumerate(plan[:5], 1):
            facts = [f for q, f in notes if int(q) == qi] or [f for _, f in notes[qi - 1:qi]]
            heading = re.sub(r"^(How|What|Why|Which|Who)\s+(is|are|was|were)?\s*", "", question).rstrip("?")
            sections.append(f"## {heading[:1].upper() + heading[1:]}\n\n" + " ".join(facts or ["No data found."]))
        summary = " ".join(f for _, f in notes[:2]) or "This brief summarises the available research."
        extra = f"\n\n## Update\n\nThis version applies the request: {rev.group(1).strip()}." if rev else ""
        return f"# {title}\n\n## Executive summary\n\n{summary}\n\n" + "\n\n".join(sections) + extra

    # ---------------------------------------------------------------- tools (researcher ReAct)
    def tools(self, tier, kind, messages, tools):
        time.sleep(self.delay)
        results = [m for m in messages if isinstance(m, ToolMessage)]
        topic = _field(_last_human(messages), "Topic", "topic")
        if not results:
            return AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": topic},
                                                      "id": "call_search_1"}]), 0
        urls = []
        for m in results:
            urls += re.findall(r"URL: (\S+)", str(m.content))
        read = sum(1 for m in results if str(m.content).startswith("<tool_result>\n["))
        if read == 0 and urls:
            calls = [{"name": "read_page", "args": {"url": u, "focus": topic}, "id": f"call_read_{i}"}
                     for i, u in enumerate(urls[:3])]
            internal = [t["function"]["name"] for t in tools if "__" in t["function"]["name"]]
            if internal:
                calls.append({"name": internal[0], "args": {"query": topic}, "id": "call_mcp"})
            return AIMessage(content="", tool_calls=calls), 0
        return AIMessage(content="Enough material gathered."), 0
