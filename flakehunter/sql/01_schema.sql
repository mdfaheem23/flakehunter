-- FlakeHunter: CI telemetry warehouse on Exasol Personal
--
-- Tables use CREATE TABLE IF NOT EXISTS, never CREATE OR REPLACE:
-- `ingest` re-applies this file on every run, and CREATE OR REPLACE
-- would silently drop the history of every previously ingested repo.
--
-- Grain notes:
--   job_runs   -- one row per job execution (always available from GitHub API)
--   test_runs  -- one row per test case execution (only when the repo
--                 publishes JUnit XML artifacts; may be empty)
--
-- Flake detection works at either grain. The signal is identical:
-- the same commit SHA producing more than one distinct outcome.

CREATE SCHEMA IF NOT EXISTS FLAKEHUNTER;
OPEN SCHEMA FLAKEHUNTER;

-- ---------------------------------------------------------------
-- Workflow runs: one row per CI run triggered on a commit
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id          DECIMAL(18,0)   NOT NULL,
    repo            VARCHAR(255)    NOT NULL,
    workflow_name   VARCHAR(255),
    commit_sha      CHAR(40)        NOT NULL,
    branch          VARCHAR(255),
    event           VARCHAR(64),      -- push, pull_request, schedule...
    status          VARCHAR(32),      -- completed, in_progress
    conclusion      VARCHAR(32),      -- success, failure, cancelled
    run_attempt     DECIMAL(4,0),     -- >1 means somebody hit "re-run"
    created_at      TIMESTAMP,
    started_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    actor           VARCHAR(255),
    PRIMARY KEY (run_id, run_attempt)
);

-- ---------------------------------------------------------------
-- Job runs: one row per job inside a run. This is our fallback
-- grain and it is ALWAYS populated.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_runs (
    job_id          DECIMAL(18,0)   NOT NULL PRIMARY KEY,
    run_id          DECIMAL(18,0)   NOT NULL,
    run_attempt     DECIMAL(4,0),
    repo            VARCHAR(255)    NOT NULL,
    job_name        VARCHAR(512)    NOT NULL,
    commit_sha      CHAR(40)        NOT NULL,
    branch          VARCHAR(255),
    status          VARCHAR(32),
    conclusion      VARCHAR(32),      -- success, failure, cancelled, skipped
    runner_name     VARCHAR(255),
    runner_group    VARCHAR(255),
    labels          VARCHAR(512),     -- ubuntu-latest, macos-14, ...
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    duration_sec    DECIMAL(10,2)
);

-- ---------------------------------------------------------------
-- Test runs: per-test-case grain, parsed from JUnit XML artifacts.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS test_runs (
    job_id          DECIMAL(18,0)   NOT NULL,
    run_id          DECIMAL(18,0)   NOT NULL,
    repo            VARCHAR(255)    NOT NULL,
    suite_name      VARCHAR(512),
    test_name       VARCHAR(1024)   NOT NULL,
    class_name      VARCHAR(512),
    file_path       VARCHAR(1024),
    status          VARCHAR(32)     NOT NULL,  -- passed, failed, error, skipped
    commit_sha      CHAR(40)        NOT NULL,
    branch          VARCHAR(255),
    duration_sec    DECIMAL(10,4),
    started_at      TIMESTAMP,
    runner_labels   VARCHAR(512),
    failure_type    VARCHAR(512),
    failure_message VARCHAR(2000000)           -- the unstructured half: stack traces
);

-- ---------------------------------------------------------------
-- Step-level log excerpts for failed jobs. Unstructured text that
-- Text AI / clustering runs against.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failure_logs (
    job_id          DECIMAL(18,0)   NOT NULL,
    repo            VARCHAR(255)    NOT NULL,
    step_name       VARCHAR(512),
    commit_sha      CHAR(40),
    started_at      TIMESTAMP,
    log_excerpt     VARCHAR(2000000)
);

-- ---------------------------------------------------------------
-- Agent output: what FlakeHunter concluded, so verdicts are
-- auditable and diffable across runs.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flake_verdicts (
    verdict_id      VARCHAR(64)     NOT NULL PRIMARY KEY,
    repo            VARCHAR(255)    NOT NULL,
    subject         VARCHAR(1024)   NOT NULL,  -- test or job name
    grain           VARCHAR(16)     NOT NULL,  -- 'test' | 'job'
    is_flaky        BOOLEAN,
    confidence      DECIMAL(4,3),
    root_cause      VARCHAR(2000),             -- a sentence, not a code: models write long causes
    rationale       VARCHAR(100000),
    evidence_sql    VARCHAR(100000),           -- the queries that justify it
    proposed_action VARCHAR(64),               -- patch | quarantine | report_only
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
