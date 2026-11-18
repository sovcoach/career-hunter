-- 0033_criteria_caps (search track): criteria rules have a kind, veto or cap (ADR-028).
--
-- Each rule in the criteria file now starts with its kind: `veto:` or `cap low:`,
-- `cap medium:` or `cap high:`. A rule without a valid kind holds scoring like any
-- unusable file, so `component_health.last_failure` gains `no-kind`. A cap rule lowers
-- a posting's fit to at most its level, grounded by a quote like a veto, so
-- `score_calls.outcome` gains `capped` and `cap_rejected`. SQLite can't change a CHECK
-- in place, so both tables are rebuilt with the same columns, every row copied as it
-- was (score_calls with its id), and score_calls' index created again. No path, quote
-- or file text is stored.
-- EI files are not changed by this migration.

CREATE TABLE component_health_new (
    component        TEXT    PRIMARY KEY CHECK (component IN
                         ('lm_studio', 'searxng', 'boards', 'criteria')),
    failures         INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure     TEXT    CHECK (last_failure IS NULL OR last_failure IN
                         ('unreachable', 'unauthorized', 'bad-reply', 'throttled', 'failed',
                          'outside', 'missing', 'unreadable', 'loose', 'not-utf8',
                          'too-long', 'empty', 'not-numbered', 'out-of-order',
                          'rule-too-long', 'too-many-rules', 'no-kind')),
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
         'veto_rejected', 'capped', 'cap_rejected'))
);

INSERT INTO score_calls_new
    (id, run_id, posting_id, started_at, finished_at, duration_ms, outcome)
SELECT id, run_id, posting_id, started_at, finished_at, duration_ms, outcome
FROM score_calls;

DROP TABLE score_calls;

ALTER TABLE score_calls_new RENAME TO score_calls;

CREATE INDEX ix_score_calls_started ON score_calls (started_at);
