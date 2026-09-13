"""Pull real CI history from the GitHub Actions API.

Grain strategy
--------------
Job-level data is always available from the REST API, so that is the
spine of the warehouse. Per-test-case data requires the repo to publish
JUnit XML artifacts, which many repos do not -- so test_runs is treated
as an optional enrichment, never a dependency.

Flake detection works at either grain: the signal is the same commit
producing more than one outcome.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

API = "https://api.github.com"
PER_PAGE = 100


def _token() -> str:
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not tok:
        raise RuntimeError(
            "No GitHub token. Unauthenticated requests are capped at 60/hour, "
            "which is not enough to backfill. Set GITHUB_TOKEN, or run:\n"
            "    export GITHUB_TOKEN=$(gh auth token)"
        )
    return tok


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    return round((end - start).total_seconds(), 2)


@dataclass
class Harvest:
    """Everything pulled for one repo, ready to load into Exasol."""
    repo: str
    workflow_runs: list[tuple] = field(default_factory=list)
    job_runs: list[tuple] = field(default_factory=list)

    def summary(self) -> str:
        commits = {r[3] for r in self.workflow_runs}
        return (
            f"{self.repo}: {len(self.workflow_runs)} runs, "
            f"{len(self.job_runs)} jobs, {len(commits)} distinct commits"
        )


class GitHubCollector:
    BATCH = 200          # requests in flight per wave

    def __init__(self, repo: str, concurrency: int = 8) -> None:
        self.repo = repo
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=API,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def __aenter__(self) -> "GitHubCollector":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **params: Any) -> dict:
        """GET with retry on secondary rate limits."""
        async with self._sem:
            for attempt in range(5):
                resp = await self._client.get(url, params=params)
                if resp.status_code == 403 and "rate limit" in resp.text.lower():
                    reset = int(resp.headers.get("x-ratelimit-reset", 0))
                    wait = max(reset - datetime.now(timezone.utc).timestamp(), 2)
                    await asyncio.sleep(min(wait, 60))
                    continue
                if resp.status_code in (502, 503):
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            resp.raise_for_status()
            return resp.json()

    async def workflow_runs(self, max_runs: int) -> AsyncIterator[dict]:
        """Newest-first. Includes every re-run attempt, which is where
        the flake signal lives."""
        yielded = 0
        page = 1
        while yielded < max_runs:
            data = await self._get(
                f"/repos/{self.repo}/actions/runs",
                per_page=PER_PAGE, page=page, status="completed",
            )
            runs = data.get("workflow_runs", [])
            if not runs:
                return
            for run in runs:
                yield run
                yielded += 1
                if yielded >= max_runs:
                    return
            page += 1

    async def jobs_for_run(self, run_id: int, attempt: int) -> list[dict]:
        data = await self._get(
            f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            per_page=PER_PAGE,
        )
        return data.get("jobs", [])

    async def harvest(self, max_runs: int = 1000) -> Harvest:
        out = Harvest(repo=self.repo)
        runs: list[dict] = [r async for r in self.workflow_runs(max_runs)]

        for run in runs:
            out.workflow_runs.append((
                run["id"], self.repo, run.get("name"), run["head_sha"],
                run.get("head_branch"), run.get("event"), run.get("status"),
                run.get("conclusion"), run.get("run_attempt", 1),
                _ts(run.get("created_at")), _ts(run.get("run_started_at")),
                _ts(run.get("updated_at")),
                (run.get("actor") or {}).get("login"),
            ))

        # Every attempt of every run -- a re-run that flips from failure to
        # success on the same SHA is the cleanest flake evidence there is.
        #
        # Issued in bounded batches rather than one big gather: a large repo
        # is thousands of requests, and awaiting them all at once holds every
        # response in memory simultaneously (which the OOM killer notices).
        pending = [
            (run["id"], attempt)
            for run in runs
            for attempt in range(1, int(run.get("run_attempt", 1)) + 1)
        ]
        results: list[list[dict]] = []
        for i in range(0, len(pending), self.BATCH):
            chunk = pending[i:i + self.BATCH]
            results = await asyncio.gather(
                *(self.jobs_for_run(rid, att) for rid, att in chunk),
                return_exceptions=True,
            )
            self._absorb(out, results)
        return out

    def _absorb(self, out: "Harvest", results: list) -> None:
        for batch in results:
            if isinstance(batch, BaseException):
                continue
            for job in batch:
                started, completed = _ts(job.get("started_at")), _ts(job.get("completed_at"))
                out.job_runs.append((
                    job["id"], job["run_id"], job.get("run_attempt", 1), self.repo,
                    job["name"], job["head_sha"], job.get("head_branch"),
                    job.get("status"), job.get("conclusion"),
                    job.get("runner_name"), job.get("runner_group_name"),
                    ",".join(job.get("labels") or []),
                    started, completed, _duration(started, completed),
                ))
