"""Drive the real investigation graph against the real Exasol MCP server.

The only thing stubbed is the model's *judgement*. Everything else is
live: the LangGraph loop, the structured-output contracts, the MCP tool
invocation, and the SQL running on Exasol.

This is what makes the graph trustworthy before an API key exists -- if
the wiring is wrong, this fails; if the model is merely unwise, it passes.
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from langchain_core.messages import AIMessage

from flakehunter.agent.graph import (
    Assessment, FinalVerdict, Hypotheses, ProbeResult,
    build_investigation_graph,
)
from flakehunter.agent.llm import exasol_tools

load_dotenv(".env.local")

PROBE_SQL = (
    "SELECT job_name, conclusion, runs, avg_parallel_jobs "
    "FROM FLAKEHUNTER.v_probe_concurrency "
    "WHERE repo = 'home-assistant/core' LIMIT 3"
)


class _Structured:
    """Returns whatever shape the graph asked for."""

    def __init__(self, schema, state: dict) -> None:
        self._schema, self._state = schema, state

    async def ainvoke(self, _messages):
        if self._schema is Hypotheses:
            return Hypotheses(hypotheses=[
                "shared_state: fails more when CI is under load",
                "timing: fails on slower runners",
            ])
        if self._schema is ProbeResult:
            self._state["probes"] += 1
            return ProbeResult(
                hypothesis="shared_state: fails more when CI is under load",
                sql=PROBE_SQL, supports=True,
                reasoning="failing runs overlap ~14% more parallel jobs",
                settled=self._state["probes"] >= 2,   # exercise the loop edge
            )
        if self._schema is Assessment:
            # settle only after two probes, so the loop edge is exercised
            self._state["assessments"] += 1
            return Assessment(settled=self._state["assessments"] >= 2,
                              reason="one hypothesis supported, rival refuted")
        if self._schema is FinalVerdict:
            return FinalVerdict(
                is_flaky=True, confidence=0.81, root_cause="shared_state",
                rationale="Same commit produced both outcomes; failures "
                          "cluster under high parallelism.",
                proposed_action="quarantine",
            )
        raise AssertionError(f"unexpected schema {self._schema}")


class StubLLM:
    def __init__(self) -> None:
        self._state = {"assessments": 0, "probes": 0}
        self._tools: list = []

    def with_structured_output(self, schema):
        return _Structured(schema, self._state)

    def bind_tools(self, tools):
        self._tools = tools
        return self

    async def ainvoke(self, _messages):
        """Stand in for the model deciding to call the SQL tool."""
        return AIMessage(content="", tool_calls=[{
            "name": "execute_exasol_query",
            "args": {"query": PROBE_SQL},
            "id": "probe_1",
        }])


async def main() -> int:
    tools = await exasol_tools()
    assert any(t.name == "execute_exasol_query" for t in tools), \
        "MCP server exposed no query tool -- check EXA_MCP_SETTINGS"

    graph = build_investigation_graph(StubLLM(), tools)
    result = await graph.ainvoke({
        "repo": "home-assistant/core",
        "subject": "Run tests Python 3.14.5 (8)", "grain": "job",
        "baseline": "att=2 failures=1", "hypotheses": [], "probes": [],
        "rounds": 0, "max_rounds": 4, "verdict": None, "messages": [],
    })

    probes, verdict = result["probes"], result["verdict"]
    print(f"probes run: {len(probes)}")
    for i, p in enumerate(probes, 1):
        got_rows = "columns" in p["result"] or "rows" in p["result"]
        print(f"  probe {i}: supports={p['supports']} real_rows_from_exasol={got_rows}")
        print(f"    exasol returned: {p['result'][:160]}")

    print(f"\nverdict: flaky={verdict['is_flaky']} conf={verdict['confidence']} "
          f"cause={verdict['root_cause']} action={verdict['proposed_action']}")

    assert len(probes) >= 2, "conditional edge did not loop"
    assert any("rows" in p["result"] for p in probes), "no real data came back from Exasol"
    assert verdict["root_cause"] == "shared_state"
    print("\nPASS: graph loop + MCP + Exasol verified end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
