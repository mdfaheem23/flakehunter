"""Graph state for the FlakeHunter investigation loop."""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage

RootCause = Literal[
    "timing",             # waits on a clock instead of a condition
    "shared_state",       # two tests touch the same resource
    "order_dependency",   # only passes if another test ran first
    "external_service",   # depends on a network call it does not control
    "resource_limit",     # OOM / disk / runner capacity
    "real_regression",    # NOT flaky -- the code is genuinely broken
    "unknown",
]


class Probe(TypedDict):
    """One question the agent asked the database, and what came back."""
    hypothesis: str
    sql: str
    result: str
    supports: bool          # did the data back the hypothesis?
    reasoning: str
    settled: bool           # is the investigation conclusive after this probe?


class Verdict(TypedDict):
    subject: str
    grain: Literal["job", "test"]
    is_flaky: bool
    confidence: float
    root_cause: RootCause
    rationale: str
    proposed_action: Literal["patch", "quarantine", "report_only"]
    probes: list[Probe]


class InvestigationState(TypedDict):
    """State for investigating ONE subject.

    The probe list is the agent's working memory: what it has already
    asked, so it does not go in circles, and what it can cite as evidence.
    """
    repo: str
    subject: str
    grain: Literal["job", "test"]
    baseline: str                                   # triage stats for context
    profile: str                                    # which tables actually hold rows
    hypotheses: list[str]                           # still open
    probes: Annotated[list[Probe], operator.add]    # accumulated evidence
    rounds: int
    max_rounds: int
    verdict: Verdict | None
    messages: Annotated[list[BaseMessage], operator.add]


class HuntState(TypedDict):
    """Top-level state across all subjects in a repo."""
    repo: str
    grain: Literal["job", "test"]
    candidates: list[dict]
    verdicts: Annotated[list[Verdict], operator.add]
    max_subjects: int
    report: str
