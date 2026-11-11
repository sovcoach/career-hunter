-- 0002_job_descriptions (ei_record track): the job description is part of the record
-- (ADR-024).
--
-- This migration only adds. It never UPDATEs a row, so a voided (frozen) application
-- doesn't stop it, and no audit trigger fires. A sealed claim's file is never migrated:
-- every reader handles a file still at 0001.
--
-- applications.jd_expected says whether a JD was expected when the application was
-- recorded. Rows from before this migration get 0 (pre-feature); every writer from now
-- on sets 1. It never changes, so it needs no audit entry: the trigger below refuses
-- any change.
--
-- job_descriptions holds each JD verbatim, with the SHA-256 of that exact text, where
-- it came from (jd_source: the column is not called `source`, a name the EI schema
-- keeps for no column, since search.db uses it for where a posting was found), and its
-- snapshot file (relative to the data folder, under snapshots/).
-- It is append-only: a corrected JD is a new row, and the latest row (highest id) is the
-- current JD. Its rows follow the record's rules: no deletes, no updates, nothing added
-- once the claim is sealed, and nothing added to a voided application.

ALTER TABLE applications
    ADD COLUMN jd_expected INTEGER NOT NULL DEFAULT 0 CHECK (jd_expected IN (0, 1));

CREATE TRIGGER fixed_jd_expected_applications BEFORE UPDATE OF jd_expected ON applications
WHEN OLD.jd_expected IS NOT NEW.jd_expected
BEGIN SELECT RAISE(ABORT, 'jd_expected can not be changed'); END;

CREATE TABLE job_descriptions (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    text           TEXT    NOT NULL CHECK (length(text) > 0),
    text_sha256    TEXT    NOT NULL CHECK (
                       length(text_sha256) = 64 AND text_sha256 NOT GLOB '*[^0-9a-f]*'),
    jd_source      TEXT    NOT NULL CHECK (jd_source IN ('pasted', 'file', 'fetched')),
    file_path      TEXT    CHECK (file_path IS NULL OR (
                       file_path GLOB 'snapshots/*' AND instr(file_path, '..') = 0)),
    file_sha256    TEXT    CHECK (file_sha256 IS NULL OR (
                       length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*')),
    file_type      TEXT    CHECK (file_type IS NULL OR file_type IN ('pdf', 'html', 'txt')),
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (
        (file_path IS NULL AND file_sha256 IS NULL AND file_type IS NULL) OR
        (file_path IS NOT NULL AND file_sha256 IS NOT NULL AND file_type IS NOT NULL)
    ),
    UNIQUE (application_id, text_sha256)
);

CREATE TRIGGER no_delete_job_descriptions BEFORE DELETE ON job_descriptions
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_update_job_descriptions BEFORE UPDATE ON job_descriptions
BEGIN SELECT RAISE(ABORT, 'job_descriptions is append-only: add a new row instead'); END;
CREATE TRIGGER sealed_insert_job_descriptions BEFORE INSERT ON job_descriptions
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER voided_insert_job_descriptions BEFORE INSERT ON job_descriptions
WHEN (SELECT voided FROM applications WHERE id = NEW.application_id) = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;
