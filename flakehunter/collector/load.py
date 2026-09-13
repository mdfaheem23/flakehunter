"""Load harvested CI data into Exasol Personal.

Uses pyexasol's native bulk import (import_from_iterable) rather than
row-by-row INSERT -- the point of this project is that the warehouse
holds enough history for patterns to be visible, so ingestion has to
handle hundreds of thousands of rows without complaint.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pyexasol

from .github import Harvest

SCHEMA = "FLAKEHUNTER"
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def connect() -> pyexasol.ExaConnection:
    dsn = os.getenv("EXA_DSN", "127.0.0.1:8563")
    user = os.getenv("EXA_USER", "sys")
    password = os.getenv("EXA_PASSWORD", "exasol")
    return pyexasol.connect(
        dsn=dsn, user=user, password=password,
        compression=True,
        # Exasol Personal ships a self-signed cert by default
        encryption=True,
        websocket_sslopt={"cert_reqs": 0},
    )


def apply_sql_file(conn: pyexasol.ExaConnection, path: Path) -> None:
    """Execute a .sql file statement by statement so one failure is
    attributable to a specific statement."""
    # Strip line comments FIRST -- a ";" inside a comment would otherwise
    # split a statement in half.
    lines = [
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    raw = "\n".join(lines)
    for stmt in (s.strip() for s in raw.split(";")):
        if stmt:
            conn.execute(stmt)


def init_schema(conn: pyexasol.ExaConnection) -> None:
    for name in ("01_schema.sql", "02_analysis.sql"):
        apply_sql_file(conn, SQL_DIR / name)


def load(conn: pyexasol.ExaConnection, harvest: Harvest) -> dict[str, int]:
    conn.execute(f"OPEN SCHEMA {SCHEMA}")

    # Idempotent re-ingest: clear this repo's rows before reloading so
    # re-running the collector does not double-count and corrupt the
    # "same commit, two outcomes" signal.
    for table in ("workflow_runs", "job_runs"):
        conn.execute(f"DELETE FROM {table} WHERE repo = {{r}}", {"r": harvest.repo})

    counts: dict[str, int] = {}
    for table, rows in (("workflow_runs", harvest.workflow_runs),
                        ("job_runs", harvest.job_runs)):
        if rows:
            counts[table] = _bulk_insert(conn, table, rows)

    conn.commit()
    return counts


def _bulk_insert(conn: pyexasol.ExaConnection, table: str, rows: list[tuple],
                 chunk: int = 500) -> int:
    """Load rows into `table`.

    Prefers pyexasol's CSV HTTP transport, which is far faster. That
    requires the database to open a connection back to this process --
    impossible when Exasol Personal runs inside a local VM on its own
    network. So we fall back to chunked multi-row INSERT, which always
    works regardless of topology.
    """
    try:
        conn.import_from_iterable(rows, (SCHEMA, table))
        return len(rows)
    except Exception:                    # noqa: BLE001 -- topology-dependent
        try:
            conn.rollback()
        except Exception:                # noqa: BLE001
            pass

    prepared = [tuple(_sql_value(v) for v in row) for row in rows]
    for i in range(0, len(prepared), chunk):
        conn.ext.insert_multi(table, prepared[i:i + chunk])
    return len(rows)


def _sql_value(value):
    """insert_multi serialises parameters as JSON, which has no datetime.

    Exasol parses TIMESTAMP from 'YYYY-MM-DD HH:MI:SS.FF6', so timestamps
    are normalised to naive UTC and formatted. Everything else passes through.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return value
