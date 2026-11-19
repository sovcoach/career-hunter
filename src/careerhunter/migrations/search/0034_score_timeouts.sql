-- 0034_score_timeouts (search track): a scoring call that ran out of time is a timeout.
--
-- A call that reached `scoring.timeout_seconds` with the model still working was
-- recorded as `unreachable`, like a refused connection. It is now its own outcome:
-- `score_calls.outcome` gains `timeout`, and `component_health.last_failure` gains
-- `timeout` for a run whose scoring calls (two or more) all timed out. SQLite can't
-- change a CHECK in place, so both tables are rebuilt with the same columns, every row
-- copied as it was (score_calls with its id), and score_calls' index created again.
-- Rows recorded as `unreachable` before this migration keep that outcome.
--
-- `score_timeouts` has one row per watcher or discovery posting whose last scoring
-- calls timed out: `timeouts` counts them in a row, and `last_timed_out_at` is the last
-- one. After `watch.TIMEOUTS_TO_SET_ASIDE` in a row the posting is set aside
-- (`set_aside_at`): it is not sent to the model again until `careerhunter rescore
-- --set-aside` clears it, and `doctor` counts it. The row is removed once a call for
-- the posting is answered. A refused connection or a budget cut is not counted here.
-- No path, prompt, reply or text is stored.
-- EI files are not changed by this migration.

CREATE TABLE component_health_new (
    component        TEXT    PRIMARY KEY CHECK (component IN
                         ('lm_studio', 'searxng', 'boards', 'criteria')),
    failures         INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure     TEXT    CHECK (last_failure IS NULL OR last_failure IN
                         ('unreachable', 'unauthorized', 'bad-reply', 'throttled', 'failed',
                          'outside', 'missing', 'unreadable', 'loose', 'not-utf8',
                          'too-long', 'empty', 'not-numbered', 'out-of-order',
                          'rule-too-long', 'too-many-rules', 'no-kind', 'timeout')),
    held             INTEGER CHECK (held IS NULL OR held >= 0),
    updated_at       TEXT    CHECK (updated_at IS NULL OR updated_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    alerted_failures INTEGER CHECK (alerted_failures IS NULL OR alerted_failures >= 1),
    recovery_due     INTEGER NOT NULL DEFAULT 0 CHECK (recovery_due IN (0, 1)),
    CHECK ((failures = 0) = (last_failure IS NULL))
);

INSERT INTO component_health_new
    (component, failures, last_failure, held, updated_at, alerted_failures, recovery_due)
SELECT component, failures, last_failure, held, updated_at, alerted_failures, recovery_due
FROM component_health;

DROP TABLE component_health;

ALTER TABLE component_health_new RENAME TO component_health;

CREATE TABLE score_calls_new (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES watch_runs (id),
    posting_id  INTEGER NOT NULL REFERENCES postings (id),
    started_at  TEXT    NOT NULL CHECK (started_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    finished_at TEXT    NOT NULL CHECK (finished_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    outcome     TEXT    NOT NULL CHECK (outcome IN
        ('scored', 'unparseable', 'unreachable', 'unauthorized', 'budget', 'vetoed',
         'veto_rejected', 'capped', 'cap_rejected', 'timeout'))
);

INSERT INTO score_calls_new
    (id, run_id, posting_id, started_at, finished_at, duration_ms, outcome)
SELECT id, run_id, posting_id, started_at, finished_at, duration_ms, outcome
FROM score_calls;

DROP TABLE score_calls;

ALTER TABLE score_calls_new RENAME TO score_calls;

CREATE INDEX ix_score_calls_started ON score_calls (started_at);

CREATE TABLE score_timeouts (
    posting_id        INTEGER PRIMARY KEY REFERENCES postings (id),
    timeouts          INTEGER NOT NULL CHECK (timeouts >= 1),
    last_timed_out_at TEXT    NOT NULL CHECK (last_timed_out_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    set_aside_at      TEXT    CHECK (set_aside_at IS NULL OR set_aside_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);
