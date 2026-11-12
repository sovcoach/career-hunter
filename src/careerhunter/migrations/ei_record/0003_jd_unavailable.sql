-- 0003_jd_unavailable (ei_record track): an application whose posting no longer exists,
-- so its job description can't be kept (ADR-024, amended).
--
-- This migration only adds. A sealed claim's file is never migrated: every reader
-- handles a file still at 0001 or 0002.
--
-- jd_unavailable holds one row per time the operator marked an application's JD as
-- unavailable, with an explanation (for example "posting removed before capture"):
-- trimmed text of 1 to 200 characters. The column is not called `reason`, a name the EI
-- schema keeps for no column, since search.db uses it for skips. It is append-only: a new reason is a new row, and the
-- latest row (highest id) is the current one. A JD added later is recorded as usual in
-- job_descriptions and takes the marker's place wherever the JD is shown; the marker
-- stays in the record. Its rows follow the record's rules: no deletes, no updates,
-- nothing added once the claim is sealed, and nothing added to a voided application.

CREATE TABLE jd_unavailable (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    explanation    TEXT    NOT NULL CHECK (
                       length(explanation) BETWEEN 1 AND 200
                       AND explanation = trim(explanation)),
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TRIGGER no_delete_jd_unavailable BEFORE DELETE ON jd_unavailable
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_update_jd_unavailable BEFORE UPDATE ON jd_unavailable
BEGIN SELECT RAISE(ABORT, 'jd_unavailable is append-only: add a new row instead'); END;
CREATE TRIGGER sealed_insert_jd_unavailable BEFORE INSERT ON jd_unavailable
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER voided_insert_jd_unavailable BEFORE INSERT ON jd_unavailable
WHEN (SELECT voided FROM applications WHERE id = NEW.application_id) = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;
