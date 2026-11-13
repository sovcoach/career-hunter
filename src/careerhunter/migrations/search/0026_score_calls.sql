-- 0026_score_calls (search track): each scoring call's start, end and duration.
--
-- One row per call to the local chat model in a watch run, written when the call ends:
-- `outcome` is `scored` (a reply that parsed), `unparseable` (a reply that did not),
-- `unreachable` or `unauthorized` (no usable answer), or `budget` (the run's budget ran
-- out during the call). `duration_ms` is measured on a monotonic clock, so a change of
-- the wall clock during a call does not distort it. doctor reports the median and the
-- maximum of recent answered calls beside scoring.timeout_seconds, so that setting can be
-- tuned from data. No prompt, reply, posting text or model output is stored here.
-- EI files are not changed by this migration.

CREATE TABLE score_calls (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES watch_runs (id),
    posting_id  INTEGER NOT NULL REFERENCES postings (id),
    started_at  TEXT    NOT NULL CHECK (started_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    finished_at TEXT    NOT NULL CHECK (finished_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    outcome     TEXT    NOT NULL CHECK (outcome IN
        ('scored', 'unparseable', 'unreachable', 'unauthorized', 'budget'))
);
CREATE INDEX ix_score_calls_started ON score_calls (started_at);
