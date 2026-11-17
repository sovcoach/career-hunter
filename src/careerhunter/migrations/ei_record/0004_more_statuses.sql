-- 0004_more_statuses (ei_record track): two more statuses in the shared pipeline.
--
-- `assessment_requested`: the employer asked for an assessment (a test, a skills or
-- physical assessment), before one is submitted. `position_closed`: the employer
-- closed or filled the position without a decision on this application. Both are
-- statuses the employer reports, so they belong in the record like the others.
--
-- SQLite can't change a CHECK in place, so status_history is rebuilt with the same
-- columns and every row copied as it was: the same ids, dates and recording times.
-- Its index and its three triggers (no deletes, no updates, nothing added once
-- sealed) are created again, unchanged. A sealed claim's file is never migrated:
-- every reader handles a file still at an earlier version.

CREATE TABLE status_history_new (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('applied', 'screening', 'assessment_requested',
                        'assessment_submitted', 'interview_scheduled', 'offer', 'hired',
                        'rejected', 'no_response', 'withdrawn', 'position_closed')),
    -- NULL only when the date is genuinely unknown (e.g. an undated seed application).
    status_date    TEXT    CHECK (status_date IS NULL OR date(status_date) IS status_date),
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO status_history_new (id, application_id, status, status_date, recorded_at)
SELECT id, application_id, status, status_date, recorded_at FROM status_history;

DROP TABLE status_history;

ALTER TABLE status_history_new RENAME TO status_history;

CREATE INDEX ix_status_history_application ON status_history (application_id, id);

CREATE TRIGGER no_delete_status_history BEFORE DELETE ON status_history
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_update_status_history BEFORE UPDATE ON status_history
BEGIN SELECT RAISE(ABORT, 'status_history is append-only: add a new status row instead'); END;
CREATE TRIGGER sealed_insert_status_history BEFORE INSERT ON status_history
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
