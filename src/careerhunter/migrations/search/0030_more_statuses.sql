-- 0030_more_statuses (search track): two more statuses in the shared pipeline.
--
-- `assessment_requested` (the employer asked for an assessment) and `position_closed`
-- (the employer closed or filled the position without a decision on this
-- application), the same as EI migration 0004. SQLite can't change a CHECK in place,
-- so decision_history is rebuilt with the same columns, every row copied as it was,
-- and its index created again. EI files are migrated by their own track.

CREATE TABLE decision_history_new (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('applied', 'screening', 'assessment_requested',
                        'assessment_submitted', 'interview_scheduled', 'offer', 'hired',
                        'rejected', 'no_response', 'withdrawn', 'position_closed')),
    status_date    TEXT    CHECK (status_date IS NULL OR date(status_date) IS status_date),
    note           TEXT,
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO decision_history_new (id, application_id, status, status_date, note, recorded_at)
SELECT id, application_id, status, status_date, note, recorded_at FROM decision_history;

DROP TABLE decision_history;

ALTER TABLE decision_history_new RENAME TO decision_history;

CREATE INDEX ix_decision_history_application ON decision_history (application_id, id);
