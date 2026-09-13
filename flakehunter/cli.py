"""FlakeHunter command line.

    flakehunter init                      -- create schema + analysis views
    flakehunter ingest --repo owner/name  -- pull real CI history into Exasol
    flakehunter hunt   --repo owner/name  -- run the agent, print verdicts
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent.graph import build_investigation_graph
from .agent.llm import build_llm, exasol_tools
from .collector.github import GitHubCollector
from .collector.load import connect, init_schema, load

console = Console()

TRIAGE_SQL = """
SELECT subject, flaky_commits, total_attempts, total_failures,
       failure_rate, flake_score
FROM   FLAKEHUNTER.v_flake_score
WHERE  repo = '{repo}'
ORDER  BY flake_score DESC
LIMIT  {top}
"""


def cmd_init(_: argparse.Namespace) -> int:
    with connect() as conn:
        init_schema(conn)
    console.print("[green]Schema FLAKEHUNTER created with analysis views.[/green]")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    async def run() -> None:
        console.print(f"Harvesting [cyan]{args.repo}[/cyan] (up to {args.max_runs} runs)...")
        async with GitHubCollector(args.repo) as gh:
            harvest = await gh.harvest(max_runs=args.max_runs)
        console.print(f"  {harvest.summary()}")

        with connect() as conn:
            init_schema(conn)
            counts = load(conn, harvest)
        console.print(f"[green]Loaded into Exasol:[/green] {counts}")

    asyncio.run(run())
    return 0


def triage(repo: str, top: int) -> list[dict]:
    """Find the subjects worth investigating. Pure SQL -- no model needed."""
    with connect() as conn:
        rows = conn.execute(TRIAGE_SQL.format(repo=repo, top=top)).fetchall()
    return [
        {
            "subject": r[0], "flaky_commits": r[1], "total_attempts": r[2],
            "total_failures": r[3], "failure_rate": float(r[4] or 0),
            "flake_score": float(r[5] or 0),
        }
        for r in rows
    ]


def data_profile() -> str:
    """Row counts per table, so the agent does not mine empty tables.

    Without this the model happily queries `failure_logs`, gets an error or
    zero rows, and then invents findings to fill the gap.
    """
    tables = ("workflow_runs", "job_runs", "test_runs", "failure_logs")
    lines = []
    with connect() as conn:
        conn.execute("OPEN SCHEMA FLAKEHUNTER")
        for table in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchval()
            state = "POPULATED" if n else "EMPTY -- do not query"
            lines.append(f"  {table.upper():15} {n:>8} rows   {state}")
            if n:
                # Read the real columns rather than trusting the model to
                # guess them -- it kept inventing CREATED_AT on JOB_RUNS and
                # burning a round per mistake.
                cols = [r[0] for r in conn.execute(
                    "SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS "
                    "WHERE COLUMN_SCHEMA='FLAKEHUNTER' AND COLUMN_TABLE=:t "
                    "ORDER BY COLUMN_ORDINAL_POSITION".replace(":t", f"'{table.upper()}'")
                ).fetchall()]
                lines.append(f"      columns: {', '.join(cols)}")
    return "\n".join(lines)


def persist_verdict(repo: str, verdict: dict) -> None:
    vid = hashlib.sha256(f"{repo}:{verdict['subject']}".encode()).hexdigest()[:32]
    evidence = "\n\n".join(p["sql"] for p in verdict["probes"])
    with connect() as conn:
        conn.execute("DELETE FROM FLAKEHUNTER.flake_verdicts WHERE verdict_id = {v}", {"v": vid})
        conn.execute(
            """INSERT INTO FLAKEHUNTER.flake_verdicts
               (verdict_id, repo, subject, grain, is_flaky, confidence,
                root_cause, rationale, evidence_sql, proposed_action)
               VALUES ({v}, {r}, {s}, {g}, {f}, {c}, {rc}, {ra}, {e}, {a})""",
            {
                "v": vid, "r": repo, "s": verdict["subject"], "g": verdict["grain"],
                "f": verdict["is_flaky"], "c": verdict["confidence"],
                "rc": verdict["root_cause"], "ra": verdict["rationale"],
                "e": evidence, "a": verdict["proposed_action"],
            },
        )
        conn.commit()


def cmd_hunt(args: argparse.Namespace) -> int:
    candidates = triage(args.repo, args.top)
    if not candidates:
        console.print(
            "[yellow]No flaky candidates found.[/yellow] Either this repo's CI is "
            "healthy, or not enough history was ingested for the same commit to "
            "have run twice. Try a larger --max-runs on ingest."
        )
        return 0

    table = Table(title=f"Triage: {args.repo}", header_style="bold")
    for col in ("subject", "flaky commits", "attempts", "failures", "score"):
        table.add_column(col)
    for c in candidates:
        table.add_row(
            c["subject"][:60], str(c["flaky_commits"]), str(c["total_attempts"]),
            str(c["total_failures"]), f"{c['flake_score']:.2f}",
        )
    console.print(table)

    profile = data_profile()
    console.print(f"[dim]data profile:\n{profile}[/dim]\n")

    async def run() -> None:
        llm = build_llm()
        tools = await exasol_tools()
        console.print(f"[dim]Exasol MCP tools loaded: {[t.name for t in tools]}[/dim]\n")
        graph = build_investigation_graph(llm, tools)

        for c in candidates:
            console.rule(f"[bold cyan]{c['subject']}")
            result = await graph.ainvoke({
                "repo": args.repo, "subject": c["subject"], "grain": "job",
                "baseline": str(c), "profile": profile,
                "hypotheses": [], "probes": [],
                "rounds": 0, "max_rounds": args.max_rounds,
                "verdict": None, "messages": [],
            })
            v = result.get("verdict")
            if not v:
                console.print("[yellow]No verdict reached.[/yellow]")
                continue

            for i, p in enumerate(v["probes"], 1):
                mark = "[green]SUPPORTED[/green]" if p["supports"] else "[red]REFUTED[/red]"
                console.print(f"  probe {i} {mark}  {p['hypothesis'][:80]}")
                console.print(f"    [dim]{p['reasoning'][:200]}[/dim]")

            verdict_color = "red" if v["is_flaky"] else "green"
            console.print(Panel(
                f"[bold]{'FLAKY' if v['is_flaky'] else 'NOT FLAKY'}[/bold]  "
                f"confidence {v['confidence']:.2f}\n"
                f"cause: {v['root_cause']}\n"
                f"action: {v['proposed_action']}\n\n{v['rationale']}",
                border_style=verdict_color,
            ))
            persist_verdict(args.repo, v)

    asyncio.run(run())
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env.local")
    load_dotenv()

    parser = argparse.ArgumentParser(prog="flakehunter")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--repo", required=True, help="owner/name")
    p_ing.add_argument("--max-runs", type=int, default=500)
    p_ing.set_defaults(func=cmd_ingest)

    p_hunt = sub.add_parser("hunt")
    p_hunt.add_argument("--repo", required=True)
    p_hunt.add_argument("--top", type=int, default=3)
    p_hunt.add_argument("--max-rounds", type=int, default=6)
    p_hunt.set_defaults(func=cmd_hunt)

    from .act import cmd_act

    p_act = sub.add_parser("act", help="open a quarantine PR on your fork for a flaky verdict")
    p_act.add_argument("--repo", required=True, help="upstream owner/name")
    p_act.add_argument("--subject", required=True, help="job name from the verdict")
    p_act.add_argument("--min-confidence", type=float, default=0.7)
    p_act.add_argument("--dry-run", action="store_true",
                       help="show the diff and PR text without forking or opening anything")
    p_act.set_defaults(func=cmd_act)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
