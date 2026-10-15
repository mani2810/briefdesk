"""Structured outputs. Every LLM decision is validated against one of these (Groq json_schema mode)."""
from typing import Literal

from pydantic import BaseModel, Field

Worker = Literal["researcher", "writer", "reviewer", "verifier", "FINISH"]


class Plan(BaseModel):
    title: str = Field(description="short report title, max 12 words")
    sub_questions: list[str] = Field(description="3 to 6 research sub-questions that together cover the topic")


class RouteDecision(BaseModel):
    thought: str = Field(description="one or two sentences of reasoning over the shared state")
    next: Worker
    reason: str = Field(description="why this worker, in one short sentence")


class Finding(BaseModel):
    claim: str = Field(description="one factual statement taken from the sources, with figures if available")
    sources: list[int] = Field(description="source numbers [n] that support the claim")
    sub_question: int = Field(description="1-based index of the plan sub-question it answers, 0 if none")


class Findings(BaseModel):
    findings: list[Finding]
    gaps: list[str] = Field(description="sub-questions or details the sources did not answer")


class Review(BaseModel):
    verdict: Literal["approve", "research", "rewrite"]
    feedback: str = Field(description="specific, actionable feedback; empty if approved")
    missing: list[str] = Field(description="facts that must be researched (only for verdict=research)")


class ClaimCheck(BaseModel):
    id: int
    supported: bool


class ClaimChecks(BaseModel):
    checks: list[ClaimCheck]


Intent = Literal["start", "steer", "approve_plan", "edit_plan", "question", "revise", "export", "cancel",
                 "retry", "smalltalk"]


class ChatIntent(BaseModel):
    intent: Intent
    instruction: str = Field(description="the user's request restated as a clear instruction")
    format: str = Field(description="requested file format for export (pdf, docx, pptx, png, xlsx ...), else ''")
    scope: Literal["full", "summary", "section"] = Field(description="what part of the report to export")
    section: str = Field(description="section heading when scope=section, else ''")


class RevisedPlan(BaseModel):
    title: str
    sub_questions: list[str]
    change_summary: str = Field(description="what you changed, one sentence")


class ExportSpec(BaseModel):
    format: str = Field(description="file extension the user wants: pdf, docx, pptx, png, jpg, xlsx, csv, md, "
                                    "html, json, txt - or the closest match")
    scope: Literal["full", "summary", "section"]
    section: str
    style: str = Field(description="style hints from the user (e.g. 'one page', 'dark', '5 slides'), else ''")


class Slide(BaseModel):
    title: str
    bullets: list[str] = Field(description="3 to 5 short bullets, max 14 words each, keep [n] citations")
    notes: str = Field(description="speaker notes, 1-3 sentences")


class SlideDeck(BaseModel):
    title: str
    subtitle: str
    slides: list[Slide]


class StatCard(BaseModel):
    value: str = Field(description="a figure from the report, e.g. '71%' or '₹540'")
    label: str = Field(description="what the figure means, max 6 words")


class KeyPoint(BaseModel):
    headline: str = Field(description="max 8 words")
    detail: str = Field(description="max 22 words")


class Infographic(BaseModel):
    title: str
    subtitle: str
    stats: list[StatCard] = Field(description="2 to 4 headline figures taken from the report")
    points: list[KeyPoint] = Field(description="3 to 5 key points")
    chart_title: str = Field(description="title of a bar chart if the report has comparable numbers, else ''")
    chart_labels: list[str]
    chart_values: list[float]
    chart_unit: str


class Table(BaseModel):
    name: str = Field(description="sheet name, max 28 characters")
    columns: list[str]
    rows: list[list[str]]


class DataTables(BaseModel):
    tables: list[Table]
