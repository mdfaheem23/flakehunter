"""The FlakeHunter investigation graph.

    triage -> hypothesize -> probe -> assess --(more evidence needed)--> probe
                                        |
                                   (settled)
                                        v
                                    conclude -> persist

The loop is the product. Each pass around probe/assess is one question
put to Exasol, and the accumulated probes are both the agent's working
memory and the evidence attached to the pull request.
"""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from . import prompts
from .state import InvestigationState, Probe, Verdict


# --------------------------------------------------------------------
# Structured outputs -- the model must commit to a shape, not prose.
# --------------------------------------------------------------------
class Hypotheses(BaseModel):
    hypotheses: list[str] = Field(
        description="Competing explanations, most likely first, each with the "
                    "query that would refute it."
    )


class ProbeResult(BaseModel):
    hypothesis: str = Field(description="Which hypothesis this probe tests")
    sql: str = Field(description="The SQL that was run")
    supports: bool = Field(description="Did the data support the hypothesis?")
    reasoning: str = Field(description="What the numbers actually show")
    settled: bool = Field(
        default=False,
        description="True only if one explanation is now supported AND its "
                    "rivals are refuted AND real_regression is ruled out. "
                    "False if a query failed or more evidence is needed.",
    )


class Assessment(BaseModel):
    settled: bool = Field(description="Is the evidence sufficient for a verdict?")
    reason: str


class FinalVerdict(BaseModel):
    is_flaky: bool
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str
    rationale: str = Field(description="Explanation a developer can act on")
    proposed_action: Literal["patch", "quarantine", "report_only"]


SQL_TOOL = "execute_exasol_query"
# Tool turns per round: enough to describe a table and then query it, but
# bounded, because every turn is a model call on a rate-limited free tier.
self_max_tool_turns = 3


def _query_failed(text: str) -> bool:
    """Did the MCP tool return an error rather than a result set?"""
    lowered = text.lower()
    return any(marker in lowered for marker in (
        "query failed", "error calling tool", "a database error occurred",
        "validation error", "not found", "syntax error",
    ))


def _has_rows(text: str) -> bool:
    return '"rows"' in text or "'rows'" in text


def _format_evidence(probes: list[Probe]) -> str:
    if not probes:
        return "(none yet -- this is the first probe)"
    lines = []
    for i, p in enumerate(probes, 1):
        mark = "SUPPORTED" if p["supports"] else "REFUTED"
        lines.append(
            f"[{i}] {p['hypothesis']}\n"
            f"    verdict: {mark}\n"
            f"    sql: {p['sql']}\n"
            f"    finding: {p['reasoning']}"
        )
    return "\n".join(lines)


def build_investigation_graph(llm, tools):
    """One subject in, one verdict out."""
    sql_llm = llm.bind_tools(tools)

    async def hypothesize(state: InvestigationState) -> dict:
        msg = prompts.HYPOTHESIZE.format(
            subject=state["subject"], grain=state["grain"],
            repo=state["repo"], baseline=state["baseline"],
            profile=state.get("profile", "(unknown)"),
        )
        result = await llm.with_structured_output(Hypotheses).ainvoke(
            [SystemMessage(prompts.SYSTEM), HumanMessage(msg)]
        )
        return {"hypotheses": result.hypotheses, "rounds": 0}

    async def probe(state: InvestigationState) -> dict:
        """One question to Exasol, through the MCP SQL tool."""
        msg = prompts.PROBE.format(
            hypotheses="\n".join(f"- {h}" for h in state["hypotheses"]),
            evidence=_format_evidence(state["probes"]),
            subject=state["subject"], repo=state["repo"],
        )
        history = [SystemMessage(prompts.SYSTEM), HumanMessage(msg)]

        # Let the model use the MCP tools, then interpret what came back.
        # Metadata tools (list/describe) may take a turn or two, but a round
        # only produces evidence once real SQL has run -- otherwise a model
        # can spend its whole budget describing tables and then report that
        # queries it never executed "returned no records".
        tool_output = ""
        failures: list[str] = []
        executed_sql: list[str] = []
        for _turn in range(self_max_tool_turns):
            response = await sql_llm.ainvoke(history)
            history.append(response)
            calls = getattr(response, "tool_calls", []) or []
            if not calls:
                break
            for call in calls:
                tool = next((t for t in tools if t.name == call["name"]), None)
                if tool is None:
                    continue
                is_sql = call["name"] == SQL_TOOL
                try:
                    out = await tool.ainvoke(call["args"])
                except Exception as exc:                  # noqa: BLE001
                    out = f"QUERY FAILED: {exc}"
                text = str(out)
                if is_sql:
                    executed_sql.append(str(call["args"].get("query", "")))
                    if _query_failed(text):
                        failures.append(text)
                    tool_output += f"\n{text}"
                history.append(HumanMessage(f"Result of {call['name']}:\n{text}"))
            if executed_sql:
                break
            history.append(HumanMessage(
                f"You have the schema. Now run the probe with {SQL_TOOL}."
            ))

        if not executed_sql:
            return {
                "probes": [{
                    "hypothesis": "(no query was run)",
                    "sql": "",
                    "result": "",
                    "supports": False,
                    "reasoning": "UNPROVEN -- the model only inspected metadata "
                                 "and never queried the data this round.",
                    "settled": False,
                }],
                "rounds": state["rounds"] + 1,
            }
        attempted_sql = ";\n".join(executed_sql)

        # A query that errored is NOT evidence. Record it as a refuted probe
        # and never let the model interpret the error text as support --
        # otherwise it invents findings for tables it could not read.
        if failures and not _has_rows(tool_output):
            return {
                "probes": [{
                    "hypothesis": "(probe could not be executed)",
                    "sql": attempted_sql,
                    "result": tool_output[:4000],
                    "supports": False,
                    "reasoning": "QUERY FAILED -- no data returned, so this "
                                 f"hypothesis is unproven: {failures[0][:300]}",
                    "settled": False,
                }],
                "rounds": state["rounds"] + 1,
            }

        interpreted = await llm.with_structured_output(ProbeResult).ainvoke(
            history + [HumanMessage(
                "Record this probe: which hypothesis it tested, the SQL you "
                "ran, whether the data supported it, and what the numbers show."
                "\n\nGround every statement in the rows printed above. Quote "
                "the actual values. If the result is empty or an error, set "
                "supports=false and say the data was unavailable. Do NOT "
                "describe values that do not appear in the output."
            )]
        )
        probe_record: Probe = {
            "hypothesis": interpreted.hypothesis,
            "sql": attempted_sql,          # what ran, not what the model says ran
            "result": tool_output[:4000],
            "supports": interpreted.supports,
            "reasoning": interpreted.reasoning,
            "settled": interpreted.settled,
        }
        return {"probes": [probe_record], "rounds": state["rounds"] + 1}

    def assess(state: InvestigationState) -> dict:
        """Pure function -- the probe already told us whether it settled.

        Folding this into the probe record halves the model calls per round,
        which matters on a free tier capped at 20 requests per day.
        """
        probes = state["probes"]
        if not probes:
            return {}
        last = probes[-1]
        # A failed query never settles anything, whatever the model claims.
        if last.get("settled") and last["supports"]:
            return {"messages": [HumanMessage("assessment: evidence settled")]}
        return {}

    def route(state: InvestigationState) -> Literal["probe", "conclude"]:
        """Keep probing until the evidence settles or we hit the budget."""
        if state["rounds"] >= state["max_rounds"]:
            return "conclude"
        if not state["probes"]:
            return "probe"
        last = state["messages"][-1].content if state["messages"] else ""
        if str(last).startswith("assessment:"):
            return "conclude"
        return "probe"

    async def conclude(state: InvestigationState) -> dict:
        result = await llm.with_structured_output(FinalVerdict).ainvoke([
            SystemMessage(prompts.SYSTEM),
            HumanMessage(prompts.CONCLUDE.format(
                subject=state["subject"],
                baseline=state["baseline"],
                evidence=_format_evidence(state["probes"]),
            )),
        ])
        verdict: Verdict = {
            "subject": state["subject"],
            "grain": state["grain"],
            "is_flaky": result.is_flaky,
            "confidence": result.confidence,
            "root_cause": result.root_cause,
            "rationale": result.rationale,
            "proposed_action": result.proposed_action,
            "probes": state["probes"],
        }
        return {"verdict": verdict}

    g = StateGraph(InvestigationState)
    g.add_node("hypothesize", hypothesize)
    g.add_node("probe", probe)
    g.add_node("assess", assess)
    g.add_node("conclude", conclude)

    g.add_edge(START, "hypothesize")
    g.add_edge("hypothesize", "probe")
    g.add_edge("probe", "assess")
    g.add_conditional_edges("assess", route, {"probe": "probe", "conclude": "conclude"})
    g.add_edge("conclude", END)
    return g.compile()
