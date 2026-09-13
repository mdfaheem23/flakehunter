-- FlakeHunter analysis layer
-- These views are the agent's instruments. The agent does not invent
-- SQL from nothing; it calls these and then drills down with ad-hoc
-- queries through the Exasol MCP server.

OPEN SCHEMA FLAKEHUNTER;

-- ===============================================================
-- 1. THE PROOF
-- Same commit, more than one outcome => the code did not change,
-- so the code is not the problem. This is the ground truth signal.
-- ===============================================================
CREATE OR REPLACE VIEW v_flaky_candidates_job AS
SELECT
    repo,
    job_name                                        AS subject,
    commit_sha,
    COUNT(*)                                        AS attempts,
    COUNT(DISTINCT conclusion)                      AS distinct_outcomes,
    SUM(CASE WHEN conclusion = 'failure' THEN 1 ELSE 0 END) AS failures,
    SUM(CASE WHEN conclusion = 'success' THEN 1 ELSE 0 END) AS successes,
    MIN(started_at)                                 AS first_seen,
    MAX(started_at)                                 AS last_seen
FROM job_runs
WHERE conclusion IN ('success', 'failure')
GROUP BY repo, job_name, commit_sha
HAVING COUNT(DISTINCT conclusion) > 1;   -- passed AND failed on identical code

CREATE OR REPLACE VIEW v_flaky_candidates_test AS
SELECT
    repo,
    test_name                                       AS subject,
    commit_sha,
    COUNT(*)                                        AS attempts,
    COUNT(DISTINCT status)                          AS distinct_outcomes,
    SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failures,
    SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END)           AS successes,
    MIN(started_at)                                 AS first_seen,
    MAX(started_at)                                 AS last_seen
FROM test_runs
WHERE status IN ('passed', 'failed', 'error')
GROUP BY repo, test_name, commit_sha
HAVING COUNT(DISTINCT status) > 1;

-- ===============================================================
-- 2. FLAKE SCORE
-- Ranks candidates so the agent investigates what hurts most.
-- A test that flips on many commits is worse than one that flipped once.
-- ===============================================================
CREATE OR REPLACE VIEW v_flake_score AS
SELECT
    repo,
    subject,
    COUNT(DISTINCT commit_sha)          AS flaky_commits,
    SUM(attempts)                       AS total_attempts,
    SUM(failures)                       AS total_failures,
    ROUND(SUM(failures) / SUM(attempts), 4)          AS failure_rate,
    -- weight breadth (how many commits) over depth (failures on one commit)
    ROUND(COUNT(DISTINCT commit_sha) * LN(1 + SUM(failures)), 3) AS flake_score,
    MAX(last_seen)                      AS last_seen
FROM v_flaky_candidates_job
GROUP BY repo, subject
ORDER BY flake_score DESC;

-- ===============================================================
-- 3. THE TIME DIMENSION  (window functions)
-- Flakiness is a pattern of transitions, not a count.
-- pass->fail->pass is flaky. pass->fail->fail->fail is a real break.
-- ===============================================================
CREATE OR REPLACE VIEW v_outcome_transitions AS
SELECT
    repo,
    job_name,
    commit_sha,
    started_at,
    conclusion,
    LAG(conclusion)  OVER w AS prev_conclusion,
    LEAD(conclusion) OVER w AS next_conclusion,
    CASE
        WHEN LAG(conclusion) OVER w = 'success'
         AND conclusion = 'failure'
         AND LEAD(conclusion) OVER w = 'success'
        THEN 1 ELSE 0
    END AS is_isolated_blip
FROM job_runs
WHERE conclusion IN ('success','failure')
WINDOW w AS (PARTITION BY repo, job_name ORDER BY started_at);

-- Flip rate: how often does this job change its mind between runs?
-- High flip rate = flaky. Low flip rate with failures = genuinely broken.
CREATE OR REPLACE VIEW v_flip_rate AS
SELECT
    repo,
    job_name,
    COUNT(*)                                            AS transitions,
    SUM(CASE WHEN conclusion <> prev_conclusion THEN 1 ELSE 0 END) AS flips,
    ROUND(SUM(CASE WHEN conclusion <> prev_conclusion THEN 1 ELSE 0 END)
          / NULLIF(COUNT(*),0), 4)                      AS flip_rate,
    SUM(is_isolated_blip)                               AS isolated_blips
FROM v_outcome_transitions
WHERE prev_conclusion IS NOT NULL
GROUP BY repo, job_name;

-- ===============================================================
-- 4. HYPOTHESIS PROBES
-- One view per cause the agent can test. Each either supports or
-- refutes an explanation; the agent keeps what survives.
-- ===============================================================

-- H1: SHARED STATE -- does it fail more when CI is under load?
CREATE OR REPLACE VIEW v_probe_concurrency AS
-- Exasol rejects non-equality correlation in a correlated subselect, so
-- the overlap is expressed as a non-equi self join instead.
WITH overlap AS (
    SELECT
        j.job_id,
        j.repo,
        j.job_name,
        j.conclusion,
        COUNT(o.job_id) AS parallel_jobs
    FROM job_runs j
    LEFT JOIN job_runs o
           ON o.repo          =  j.repo
          AND o.started_at    <= j.completed_at
          AND o.completed_at  >= j.started_at
    WHERE j.conclusion IN ('success','failure')
    GROUP BY j.job_id, j.repo, j.job_name, j.conclusion
)
SELECT repo, job_name, conclusion,
       COUNT(*)                    AS runs,
       ROUND(AVG(parallel_jobs),2) AS avg_parallel_jobs
FROM overlap
GROUP BY repo, job_name, conclusion;

-- H2: TIMING -- does it fail more on slower/different runners?
CREATE OR REPLACE VIEW v_probe_runner AS
SELECT repo, job_name, labels AS runner,
       COUNT(*)                                              AS runs,
       SUM(CASE WHEN conclusion='failure' THEN 1 ELSE 0 END) AS failures,
       ROUND(SUM(CASE WHEN conclusion='failure' THEN 1 ELSE 0 END)
             / COUNT(*), 4)                                  AS failure_rate,
       ROUND(AVG(duration_sec),2)                            AS avg_duration
FROM job_runs
WHERE conclusion IN ('success','failure')
GROUP BY repo, job_name, labels;

-- H3: TIMING -- do failing runs sit near the duration ceiling?
-- A job that fails only when it runs long is waiting on a clock.
CREATE OR REPLACE VIEW v_probe_duration AS
SELECT repo, job_name, conclusion,
       COUNT(*)                                  AS runs,
       ROUND(AVG(duration_sec),2)                AS avg_sec,
       ROUND(MEDIAN(duration_sec),2)             AS median_sec,
       ROUND(MAX(duration_sec),2)                AS max_sec
FROM job_runs
WHERE conclusion IN ('success','failure') AND duration_sec IS NOT NULL
GROUP BY repo, job_name, conclusion;

-- H4: REAL REGRESSION -- did failures start on a date and never stop?
-- If so it is NOT flaky, it is broken. The agent must rule this out.
CREATE OR REPLACE VIEW v_probe_onset AS
SELECT repo, job_name,
       MIN(CASE WHEN conclusion='failure' THEN started_at END) AS first_failure,
       MAX(CASE WHEN conclusion='success' THEN started_at END) AS last_success,
       COUNT(*)                                                AS runs
FROM job_runs
WHERE conclusion IN ('success','failure')
GROUP BY repo, job_name;

-- H5: SAME ROOT CAUSE -- group failures by their error signature.
-- 40 failures across 12 jobs can be 1 problem, not 12.
CREATE OR REPLACE VIEW v_probe_error_signature AS
SELECT repo,
       failure_type,
       COUNT(*)                        AS occurrences,
       COUNT(DISTINCT test_name)       AS distinct_tests,
       COUNT(DISTINCT commit_sha)      AS distinct_commits,
       MIN(started_at)                 AS first_seen,
       MAX(started_at)                 AS last_seen
FROM test_runs
WHERE status IN ('failed','error') AND failure_type IS NOT NULL
GROUP BY repo, failure_type
ORDER BY occurrences DESC;
