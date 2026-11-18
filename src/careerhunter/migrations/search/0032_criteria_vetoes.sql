-- 0032_criteria_vetoes (search track): criteria vetoes and withdrawn alerts (ADR-028).
--
-- A scoring reply may veto the posting for one of the criteria file's rules, with a
-- quote from the posting that the code checks. `score_calls.outcome` gains `vetoed`
-- (the quote checked out, and the posting was skipped for that rule) and
-- `veto_rejected` (it did not, and the posting was scored from the same reply). SQLite
-- can't change a CHECK in place, so score_calls is rebuilt with the same columns, every
-- row copied with its id, and its index created again.
--
-- `withdrawals` holds the veto skips of postings that had already been alerted, so the
-- bot can say they are withdrawn. `sent_at` stays null until Telegram accepted the
-- message. No quote, prompt, reply or posting text is stored.
-- EI files are not changed by this migration.

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
         'veto_rejected'))
);

INSERT INTO score_calls_new
    (id, run_id, posting_id, started_at, finished_at, duration_ms, outcome)
SELECT id, run_id, posting_id, started_at, finished_at, duration_ms, outcome
FROM score_calls;

DROP TABLE score_calls;

ALTER TABLE score_calls_new RENAME TO score_calls;

CREATE INDEX ix_score_calls_started ON score_calls (started_at);

CREATE TABLE withdrawals (
    skip_id INTEGER PRIMARY KEY REFERENCES skips (id),
    sent_at TEXT    CHECK (sent_at IS NULL OR sent_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);
