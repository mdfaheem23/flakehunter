"""Act on a verdict: open a quarantine pull request on YOUR fork.

    flakehunter act --repo pytorch/pytorch --subject get_workflow_conclusion --dry-run
    flakehunter act --repo pytorch/pytorch --subject get_workflow_conclusion

Never writes to the upstream repository. The fork is created under the
authenticated account and the pull request targets the fork's own default
branch, so a human decides whether anything is ever proposed upstream.

The evidence in the PR is read straight from Exasol, not from the model's
prose -- the model's rationale is quoted, clearly labelled as such.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import os
import re
import time

import httpx
from rich.console import Console
from rich.syntax import Syntax

from .collector.load import connect

console = Console()
API = "https://api.github.com"


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

class GitHub:
    def __init__(self, token: str) -> None:
        self.client = httpx.Client(
            base_url=API, timeout=60,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def request(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kw):
        r = self.client.request(method, path, **kw)
        if r.status_code not in ok:
            try:
                detail = r.json().get("message", r.text)
            except ValueError:
                detail = r.text
            raise RuntimeError(f"GitHub {method} {path} failed ({r.status_code}): {detail}")
        return r

    def get(self, path: str, **kw) -> dict:
        return self.request("GET", path, **kw).json()


# --------------------------------------------------------------------------
# Evidence -- read from the warehouse, never from model output
# --------------------------------------------------------------------------

def load_evidence(repo: str, subject: str) -> dict:
    params = {"r": repo, "s": subject}
    with connect() as conn:
        conn.execute("OPEN SCHEMA FLAKEHUNTER")
        verdict = conn.execute(
            """SELECT is_flaky, confidence, root_cause, rationale, evidence_sql, proposed_action
               FROM flake_verdicts WHERE repo = {r} AND subject = {s}""", params,
        ).fetchone()
        if verdict is None:
            raise RuntimeError(
                f"No verdict for {subject!r} in {repo}. Run `flakehunter hunt --repo {repo}` first."
            )
        commits = conn.execute(
            """SELECT commit_sha, attempts, successes, failures
               FROM v_flaky_candidates_job WHERE repo = {r} AND subject = {s}""", params,
        ).fetchall()
        durations = conn.execute(
            """SELECT conclusion, runs, avg_sec
               FROM v_probe_duration WHERE repo = {r} AND job_name = {s}
               ORDER BY conclusion DESC""", params,
        ).fetchall()
        run_id = conn.execute(
            """SELECT run_id FROM job_runs WHERE repo = {r} AND job_name = {s}
               ORDER BY started_at DESC LIMIT 1""", params,
        ).fetchval()

    return {
        "is_flaky": bool(verdict[0]), "confidence": float(verdict[1] or 0),
        "root_cause": verdict[2], "rationale": verdict[3],
        "evidence_sql": verdict[4] or "", "action": verdict[5],
        "commits": commits, "durations": durations, "run_id": run_id,
    }


# --------------------------------------------------------------------------
# The edit -- text-level, so the workflow's comments and layout survive
# --------------------------------------------------------------------------

_KEY = re.compile(r"^(\s+)([A-Za-z0-9_-]+):\s*(#.*)?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _meaningful(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def quarantine_workflow(text: str, job_name: str, note: list[str]) -> tuple[str, str, list[str]]:
    """Add `continue-on-error: true` to the job called `job_name`.

    Matches the job key or its `name:`. Returns (new_text, job_key, dependents)
    where dependents are jobs whose `needs:` mention this job.
    """
    lines = text.splitlines(keepends=True)
    try:
        jobs_at = next(i for i, l in enumerate(lines) if re.match(r"^jobs:\s*(#.*)?$", l))
    except StopIteration:
        raise RuntimeError("Workflow has no top-level `jobs:` block.") from None

    job_indent = next(_indent(l) for l in lines[jobs_at + 1:] if _meaningful(l))

    jobs: list[tuple[str, int, int]] = []           # (key, start, end)
    for i in range(jobs_at + 1, len(lines)):
        line = lines[i]
        if _meaningful(line) and _indent(line) < job_indent:
            break
        m = _KEY.match(line)
        if m and len(m.group(1)) == job_indent:
            if jobs:
                jobs[-1] = (jobs[-1][0], jobs[-1][1], i)
            jobs.append((m.group(2), i, len(lines)))
    if jobs and jobs[-1][2] == len(lines):
        end = next((i for i in range(jobs[-1][1] + 1, len(lines))
                    if _meaningful(lines[i]) and _indent(lines[i]) < job_indent), len(lines))
        jobs[-1] = (jobs[-1][0], jobs[-1][1], end)

    def display_name(start: int, end: int) -> str | None:
        for l in lines[start + 1:end]:
            m = re.match(r"^\s+name:\s*(.+?)\s*$", l)
            if m and _indent(l) == job_indent + 2:
                return m.group(1).strip("'\"")
        return None

    match = next(((k, s, e) for k, s, e in jobs if k == job_name), None) or \
        next(((k, s, e) for k, s, e in jobs if display_name(s, e) == job_name), None)
    if match is None:
        raise RuntimeError(f"No job named {job_name!r} in this workflow.")
    key, start, end = match

    body = [l for l in lines[start + 1:end] if _meaningful(l)]
    body_indent = _indent(body[0]) if body else job_indent + 2
    if any(re.match(r"^\s*continue-on-error:", l) and _indent(l) == body_indent for l in body):
        raise RuntimeError(f"Job {key!r} already sets continue-on-error.")

    pad = " " * body_indent
    insert = [f"{pad}# {n}\n" for n in note] + [f"{pad}continue-on-error: true\n"]
    new = lines[:start + 1] + insert + lines[start + 1:]

    dependents = []
    for k, s, e in jobs:
        block = "".join(lines[s:e])
        if k != key and re.search(rf"needs:\s*(\[[^\]]*\b{re.escape(key)}\b[^\]]*\]|{re.escape(key)}\b)", block) \
                or (k != key and re.search(rf"^\s+-\s*{re.escape(key)}\s*$", block, re.M)):
            dependents.append(k)
    return "".join(new), key, dependents


# --------------------------------------------------------------------------
# PR text
# --------------------------------------------------------------------------

def pr_body(repo: str, path: str, key: str, ev: dict, dependents: list[str]) -> str:
    rows = "\n".join(
        f"| `{sha[:9]}` | {att} | {ok} | {bad} |" for sha, att, ok, bad in ev["commits"]
    ) or "| — | — | — | — |"
    dur = "\n".join(
        f"| {c} | {n} | {float(a):.2f} s |" for c, n, a in ev["durations"]
    ) or "| — | — | — |"
    dep_note = (
        f"- Jobs that depend on it (`{'`, `'.join(dependents)}`) may still be **skipped** "
        "when it fails. Quarantine keeps the workflow green; it does not make them run."
        if dependents else ""
    )
    return f"""## Quarantine `{key}` — flaky in {repo}

FlakeHunter found that **the same commit both passed and failed** this job, so the
failures are not caused by the code under test.

**Verdict:** flaky · confidence {ev['confidence']:.2f} · {ev['root_cause']}

### Evidence (queried from the Exasol warehouse)

| commit | attempts | passed | failed |
|---|---|---|---|
{rows}

| outcome | runs | average duration |
|---|---|---|
{dur}

### What this changes

Adds `continue-on-error: true` to job `{key}` in `{path}`, so a failure of this job no
longer fails the workflow.

**This does not fix the cause.** It stops the job blocking people while its owner looks
into it.

### Before merging

- Someone who owns this workflow should fix the underlying cause, then remove the quarantine.
{dep_note}

### The agent's reasoning

> {(ev['rationale'] or '').replace(chr(10), chr(10) + '> ')}

<details><summary>SQL the agent ran</summary>

```sql
{ev['evidence_sql'].strip()}
```
</details>

---
Opened by FlakeHunter on a fork. Nothing has been proposed to upstream.
"""


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------

def cmd_act(args: argparse.Namespace) -> int:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set. Run: export GITHUB_TOKEN=$(gh auth token)")

    ev = load_evidence(args.repo, args.subject)
    if not ev["is_flaky"]:
        raise RuntimeError(f"The verdict for {args.subject!r} is NOT flaky. Nothing to quarantine.")
    if ev["confidence"] < args.min_confidence:
        raise RuntimeError(
            f"Verdict confidence {ev['confidence']:.2f} is below --min-confidence "
            f"{args.min_confidence}. Investigate further before acting."
        )
    if not ev["run_id"]:
        raise RuntimeError(f"No runs of {args.subject!r} found in the warehouse.")

    gh = GitHub(token)
    upstream_owner, name = args.repo.split("/", 1)
    path = gh.get(f"/repos/{args.repo}/actions/runs/{ev['run_id']}")["path"].split("@")[0]
    console.print(f"Workflow: [cyan]{path}[/cyan]")

    note = [
        "Quarantined by FlakeHunter: the same commit both passed and failed this job.",
        f"Verdict confidence {ev['confidence']:.2f}. Fix the cause, then remove this line.",
    ]

    if args.dry_run:
        upstream = gh.get(f"/repos/{args.repo}")["default_branch"]
        f = gh.get(f"/repos/{args.repo}/contents/{path}", params={"ref": upstream})
        original = base64.b64decode(f["content"]).decode()
        patched, key, deps = quarantine_workflow(original, args.subject, note)
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))
        console.print(Syntax(diff, "diff", word_wrap=True))
        console.print(f"[dim]job key: {key}; dependents: {deps or 'none'}[/dim]\n")
        console.print(pr_body(args.repo, path, key, ev, deps))
        console.print("[yellow]Dry run: nothing was forked, committed or opened.[/yellow]")
        return 0

    me = gh.get("/user")["login"]
    fork = f"{me}/{name}"

    r = gh.request("GET", f"/repos/{fork}", ok=(200, 404))
    if r.status_code == 404:
        console.print(f"Forking {args.repo} to [cyan]{fork}[/cyan]...")
        gh.request("POST", f"/repos/{args.repo}/forks", ok=(202,),
                   json={"default_branch_only": True})
    info = None
    for _ in range(72):                                   # up to ~6 minutes
        r = gh.request("GET", f"/repos/{fork}", ok=(200, 404))
        if r.status_code == 200:
            info = r.json()
            ref = gh.request("GET", f"/repos/{fork}/git/ref/heads/{info['default_branch']}",
                             ok=(200, 404, 409))
            if ref.status_code == 200:
                break
        time.sleep(5)
    else:
        raise RuntimeError(f"Fork {fork} was not ready after 6 minutes. Run the command again.")
    if not info.get("fork") or info.get("parent", {}).get("full_name") != args.repo:
        raise RuntimeError(f"{fork} exists but is not a fork of {args.repo}. Refusing to touch it.")

    base = info["default_branch"]
    # Bring the fork up to date so the edit applies to the current workflow.
    gh.request("POST", f"/repos/{fork}/merge-upstream", ok=(200, 409, 422), json={"branch": base})
    sha = gh.get(f"/repos/{fork}/git/ref/heads/{base}")["object"]["sha"]

    branch = "flakehunter/quarantine-" + re.sub(r"[^A-Za-z0-9._-]+", "-", args.subject)
    r = gh.request("POST", f"/repos/{fork}/git/refs", ok=(201, 422),
                   json={"ref": f"refs/heads/{branch}", "sha": sha})
    if r.status_code == 422:
        branch = f"{branch}-{int(time.time())}"
        gh.request("POST", f"/repos/{fork}/git/refs",
                   json={"ref": f"refs/heads/{branch}", "sha": sha})

    f = gh.get(f"/repos/{fork}/contents/{path}", params={"ref": branch})
    original = base64.b64decode(f["content"]).decode()
    patched, key, deps = quarantine_workflow(original, args.subject, note)

    gh.request("PUT", f"/repos/{fork}/contents/{path}", json={
        "message": f"Quarantine flaky job {key}\n\nSame commit passed and failed; "
                   f"verdict confidence {ev['confidence']:.2f}. Found by FlakeHunter.",
        "content": base64.b64encode(patched.encode()).decode(),
        "sha": f["sha"], "branch": branch,
    })

    pr = gh.request("POST", f"/repos/{fork}/pulls", json={
        "title": f"Quarantine flaky job `{key}`",
        "head": branch, "base": base,
        "body": pr_body(args.repo, path, key, ev, deps),
    }).json()

    console.print(f"[green]Opened pull request on your fork:[/green] {pr['html_url']}")
    return 0
