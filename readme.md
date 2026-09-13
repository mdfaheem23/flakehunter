# FlakeHunter

**An agent that finds unreliable jobs in CI, proves why they fail, and opens a quarantine pull request so they stop blocking your team.**

Built for the Exasol AI + Data Challenge 2026 — Track 1: *AI Agents That Get Things Done*.

---

## The problem

A test should give the same answer every time. Same code in, same result out.

Some tests break that rule. They fail, you re-run them, they pass. Developers
call these *flaky*, and they are expensive in a way that compounds: an engineer
pushes correct code, CI goes red, and they spend an hour hunting a bug that was
never there. Eventually the team learns to ignore red CI — which is worse,
because now genuine failures get ignored too.

Every repository with CI has this problem. Almost nobody analyses it, because
the evidence is buried in months of build history that no ordinary tool wants
to scan.

## The approach

There is exactly one signal that *proves* flakiness:

> **The same commit produced more than one outcome.**

If commit `abc123` passed on Monday and failed on Tuesday, the code did not
change between them. So the code is not the cause. The test is.

```sql
SELECT job_name, commit_sha, COUNT(DISTINCT conclusion) AS outcomes
FROM   job_runs
GROUP  BY job_name, commit_sha
HAVING COUNT(DISTINCT conclusion) > 1;   -- passed AND failed on identical code
```

That query is the ground truth. Everything the agent does afterwards is
explaining *why*.

## How the agent works

```
  ingest            triage           investigate                act
 ────────          ────────         ─────────────             ───────
  GitHub    ──►    Exasol    ──►    LangGraph agent    ──►    quarantine PR
  Actions          SQL views        hypothesis loop           on your fork
```

The investigation loop is the product:

```
__start__ → hypothesize → probe → assess ⇄ probe
                                     ↓
                                  conclude → __end__
```

1. **hypothesize** — propose competing explanations (timing, shared state,
   test-order dependency, external service, or *not flaky at all*).
2. **probe** — pick the hypothesis the next query would most cleanly settle,
   then run that SQL against Exasol through the **Exasol MCP server**.
3. **assess** — is one explanation supported and its rivals refuted? If not,
   loop and ask another question.
4. **conclude** — deliver a verdict with the queries that justify it.

The agent is instructed to *refute* its leading hypothesis rather than confirm
it, and to rule out `real_regression` first — because telling a developer a
genuinely broken test is "just flaky" sends them chasing a ghost.

## Why Exasol

Not incidental. The agent asks large analytical questions, repeatedly, and the
loop is only usable if each one returns fast.

Measured on this machine, 13,331 jobs of real CI history:

| Query | What it does | Time |
|---|---|---|
| Flake triage | Groups every job by commit, counts distinct outcomes | **0.07s** |
| Concurrency probe | Non-equi self-join — every job against every overlapping job (~178M comparisons) | **0.34s** |

The concurrency probe is the interesting one. To test "does this fail more when
CI is under load?", every job has to be compared against every other job whose
run window overlaps it. On a transactional database that is a query you learn
not to write. Here it is fast enough to sit inside an agent loop that runs it
twenty times per investigation.

**Remove Exasol and there is no product left.**

Exasol features used:

- **Exasol MCP server** — the agent's only route to the database. Gives it
  metadata tools (list schemas, describe tables) so it can explore a warehouse
  it has not seen before, not just run canned SQL.
- **Window functions** (`LAG`/`LEAD` over partitions) — flakiness is a pattern
  of *transitions*, not a count. `pass→fail→pass` is flaky; `pass→fail→fail`
  is a real break. The `v_flip_rate` view separates them.
- **Non-equi joins** at speed — the concurrency probe above.
- Columnar aggregation across the full history on every probe.

## What it found

Warehouse: **21,654 real CI jobs** across `home-assistant/core`, `pytorch/pytorch`
and `grafana/grafana` — 517 distinct commits, no synthetic data.

A complete investigation, `pytorch/pytorch` → `get_workflow_conclusion`:

```
Triage      84 attempts on ONE commit: 82 pass, 2 fail   (flake_score 1.10)

probe 1  SUPPORTED  duration variance -> timeout
probe 2  REFUTED    runner infrastructure
probe 3  REFUTED    (query failed -- logged as unproven, not guessed)
probe 4  REFUTED    real regression

VERDICT   FLAKY   confidence 0.85
          cause:  execution duration variance leading to timeouts
          action: quarantine

          Passing runs average 4.01s. Failing runs average 34.5s -- 8.6x
          longer. The job is hitting a timeout, not failing on logic.
```

Every figure in that verdict was checked against the database by hand:
`4.01`, `34.5` and `84` are exact. The agent ruled out the runner hypothesis
using the real runner breakdown (`ubuntu-latest`, `ubuntu-24.04`), not an
assumption.

### Grounding

An earlier version of this agent invented its evidence: it queried an empty
table, got an error, and reported "failure logs indicate 403/timeouts" with
0.95 confidence — along with runner names that do not exist in the data. That
is the failure mode that makes an analysis agent worse than no agent at all.

Three changes fixed it, and they matter more than any feature here:

1. **A failed query can never become evidence.** If the MCP tool errors or
   returns no rows, the probe is recorded as unproven and the model is never
   given the chance to interpret an error message as support.
2. **The agent is told which tables hold rows, and their real columns** —
   read live from `EXA_ALL_COLUMNS` at startup. It was inventing `CREATED_AT`
   on `JOB_RUNS` (that column lives on `WORKFLOW_RUNS`) and burning a round
   per mistake.
3. **The prompt forbids stating any value not present in a result**, and
   requires quoting actual numbers.

## Does it edit test code?

It **proposes**; a person decides. `flakehunter act` takes a flaky verdict and
opens a pull request that quarantines the job (`continue-on-error: true`). The
PR is opened **on your own fork**, never on the upstream repository.

```bash
flakehunter act --repo pytorch/pytorch --subject get_workflow_conclusion --dry-run
flakehunter act --repo pytorch/pytorch --subject get_workflow_conclusion
```

Real example: [mdfaheem23/pytorch#1](https://github.com/mdfaheem23/pytorch/pull/1).

The evidence tables in the PR are queried from Exasol, not copied from the
model's prose. The model's rationale is quoted and labelled as such, along with
every SQL statement it ran — including the ones that were poor. The PR says
plainly that quarantine **does not fix the cause**, and warns when other jobs
`needs:` the quarantined one (they may still be skipped). It refuses to act on
verdicts that are not flaky or below `--min-confidence` (default 0.7).

Automatic *patches* are not built yet; today every action is a quarantine.

The recommendation depends on the agent's confidence:

- **`patch`** — the cause is mechanical and the repair is safe to write, e.g.
  replacing a fixed `sleep(2)` with a wait on the actual condition.
- **`quarantine`** — genuinely flaky, but the real fix needs someone who knows
  the codebase. The test stops blocking the team, and the developer gets the
  findings and the evidence.
- **`report_only`** — not flaky, or not enough evidence to act.

Quarantining with good evidence is a better outcome than a confident wrong
patch, and the prompts say so explicitly.

---

## Setup

### Requirements

- macOS or Linux, Python 3.13+
- [`uv`](https://docs.astral.sh/uv/)
- A GitHub token (for CI history)
- An API key for Anthropic or OpenAI — or a local Ollama model

### 1. Exasol Personal

```bash
curl https://www.exasol.com/install/ | sh
exasol install local
```

> **Needs ~10 GB free disk.** The installer creates a local VM. If the disk
> fills during installation the database comes up with unusable TLS
> certificates and hangs on connect rather than failing loudly. If that
> happens: `exasol destroy --auto-approve --remove`, free space, reinstall.

### 2. Install FlakeHunter

```bash
git clone <this repo> && cd flakehunter
uv venv && source .venv/bin/activate
uv sync
```

### 3. Configure

```bash
cp .env.example .env.local
```

The Exasol password is generated at install time:

```bash
python - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".exasol/personal/deployments/default/secrets.json"
print("EXA_PASSWORD=" + json.load(open(p))["dbPassword"])
PY
```

Then set in `.env.local`:

```ini
EXA_DSN=127.0.0.1:8563
EXA_USER=sys
EXA_PASSWORD=<from above>
EXA_SCHEMA=FLAKEHUNTER

GITHUB_TOKEN=            # export GITHUB_TOKEN=$(gh auth token)
ANTHROPIC_API_KEY=       # or OPENAI_API_KEY, or FH_LOCAL=1
```

### 4. Run

```bash
flakehunter init                                    # schema + analysis views
flakehunter ingest --repo home-assistant/core --max-runs 1000
flakehunter hunt   --repo home-assistant/core --top 3
```

---

## Notes for anyone building on Exasol Personal

Seven things that cost time and are not obvious from the docs:

1. **CSV HTTP-transport `IMPORT` does not work from the host.** pyexasol's
   `import_from_iterable` asks the database to open a connection *back* to your
   process. Exasol Personal runs inside a VM on its own network and cannot
   reach it. `load.py` falls back to chunked multi-row `INSERT`.
2. **pyexasol uses `{name}` placeholders**, not `:name`. `:name` raises
   *"Feature not supported: host parameter specification"*.
3. **No non-equality correlation in correlated subselects.** Rewrite as a
   non-equi `JOIN` — which Exasol executes very fast anyway.
4. **`insert_multi` serialises via JSON**, so `datetime` objects must be
   formatted to `'YYYY-MM-DD HH:MI:SS.FF6'` first.

5. **The MCP server ships with SQL execution switched OFF.** Out of the box
   you get 22 metadata tools and no way to run a query. Pass
   `EXA_MCP_SETTINGS={"enable_read_query": true}` and
   `execute_exasol_query` / `profile_exasol_query` appear (24 tools).
6. **Exasol Personal's self-signed certificate breaks MCP silently.** Every
   query returns the opaque message *"A database error occurred."* with no
   detail anywhere. The fix is `EXA_SSL_CERT_VALIDATION=false`.
7. **Do not use `CREATE OR REPLACE TABLE` in a schema file you re-apply.**
   `ingest` re-runs the schema each time, so it silently dropped the history
   of every previously ingested repo. Use `CREATE TABLE IF NOT EXISTS`.

## Project layout

```
flakehunter/
├── sql/
│   ├── 01_schema.sql        5 tables: workflow_runs, job_runs, test_runs,
│   │                        failure_logs, flake_verdicts
│   └── 02_analysis.sql      10 views: detection, scoring, window-function
│                            transitions, and 5 hypothesis probes
├── collector/
│   ├── github.py            async GitHub Actions harvester (rate-limit aware,
│   │                        fetches every re-run attempt)
│   └── load.py              Exasol loader with transport fallback
├── agent/
│   ├── state.py             graph state, probe + verdict types
│   ├── prompts.py           investigation prompts
│   ├── llm.py               model selection + Exasol MCP tool loading
│   └── graph.py             the LangGraph investigation loop
└── cli.py                   init / ingest / hunt
```

## Grain note

GitHub's REST API always exposes **job**-level results. **Test**-level results
require the repository to publish JUnit XML artifacts, which many do not. So
the warehouse is built on job granularity with per-test as optional enrichment.
The flake signal is identical at either grain, and re-run attempts give the
cleanest evidence available: somebody clicked *re-run*, nothing changed, and it
went green.
