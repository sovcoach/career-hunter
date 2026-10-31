-- 0001_init (ei_record track): the EI job-search record for one claim.
--
-- Each EI claim has its own file, ei_record_<label>.db. It holds only what Service
-- Canada may ask for: employer, role, location, when and how you applied, the status
-- history, other job-search activities, and references to the saved job ad and resume.
-- Fit ratings, gaps, LLM output, skip reasons, scout metadata and notes live in
-- search.db and never come here. The two files are linked only by an opaque, random
-- correlation_id on each application.
--
-- Retention: the record is kept for 6 years after the claim ends. Nothing in this schema
-- cascades, and every table refuses DELETE outright.
--
-- Corrections without losing history:
--   * applications and activities are never deleted; a mistaken one is VOIDED
--     (voided = 1 with a void_reason). Voided rows stay in the file, are left out of
--     exports, and are frozen: once voided, a row can't be changed again.
--   * every permitted UPDATE is recorded column-by-column in `edits` by the audit
--     triggers at the end of this file. `edits` itself is append-only.
--   * status_history is append-only: status changes are new rows.
--
-- Sealing: when a claim is over, `careerhunter claim seal <label>` sets claim.sealed_at.
-- From then on the triggers below refuse every INSERT and UPDATE in the file.
--
-- Dates are ISO-8601 text: 'YYYY-MM-DD' for dates, 'YYYY-MM-DDTHH:MM:SSZ' for timestamps.

-- Exactly one row: which claim this file records. The label and start never change;
-- the end can be recorded later, and the row freezes once the claim is sealed.
CREATE TABLE claim (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    label      TEXT    NOT NULL CHECK (label GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    start_date TEXT    NOT NULL CHECK (date(start_date) IS start_date),
    end_date   TEXT    CHECK (end_date IS NULL OR
                              (date(end_date) IS end_date AND end_date >= start_date)),
    sealed_at  TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (sealed_at IS NULL OR end_date IS NOT NULL)
);

-- One row per application, sanitized. Identity within the claim is employer + role +
-- location + posting url + date, so re-applying on another date is a new row.
CREATE TABLE applications (
    id             INTEGER PRIMARY KEY,
    -- Random 128-bit hex. Carries no meaning; it only links this row to search.db.
    correlation_id TEXT    NOT NULL UNIQUE CHECK (
                       length(correlation_id) = 32 AND correlation_id NOT GLOB '*[^0-9a-f]*'),
    employer       TEXT    NOT NULL COLLATE NOCASE CHECK (length(trim(employer)) > 0),
    role           TEXT    NOT NULL COLLATE NOCASE CHECK (length(trim(role)) > 0),
    location       TEXT    NOT NULL COLLATE NOCASE DEFAULT '',
    work_model     TEXT    CHECK (work_model IS NULL OR work_model IN ('onsite', 'hybrid', 'remote')),
    posting_url    TEXT,
    date_applied   TEXT    CHECK (date_applied IS NULL OR date(date_applied) IS date_applied),
    date_precision TEXT    NOT NULL CHECK (date_precision IN ('exact', 'week', 'unknown')),
    -- Always a Monday (strftime %w = 1).
    week_of        TEXT    CHECK (week_of IS NULL OR
                               (date(week_of) IS week_of AND strftime('%w', week_of) = '1')),
    method         TEXT    NOT NULL CHECK (method IN
                       ('company_site', 'linkedin_easy_apply', 'email', 'referral', 'agency',
                        'recruiter_inbound', 'other')),
    -- Relative to the data directory, so the record stays valid if the drive is renamed.
    snapshot_path  TEXT    CHECK (snapshot_path IS NULL OR substr(snapshot_path, 1, 1) <> '/'),
    resume_file    TEXT,
    voided         INTEGER NOT NULL DEFAULT 0 CHECK (voided IN (0, 1)),
    void_reason    TEXT,
    voided_at      TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (
        (date_precision = 'exact'   AND date_applied IS NOT NULL AND week_of IS NULL) OR
        (date_precision = 'week'    AND date_applied IS NULL     AND week_of IS NOT NULL) OR
        (date_precision = 'unknown' AND date_applied IS NULL     AND week_of IS NULL)
    ),
    CHECK (
        (voided = 0 AND void_reason IS NULL AND voided_at IS NULL) OR
        (voided = 1 AND length(trim(void_reason)) > 0 AND voided_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX ux_applications_natural
    ON applications (employer, role, location, coalesce(posting_url, ''), date_precision,
                     coalesce(date_applied, ''), coalesce(week_of, ''));
CREATE INDEX ix_applications_dates ON applications (date_applied, week_of);

CREATE TABLE status_history (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('applied', 'screening', 'assessment_submitted', 'interview_scheduled',
                        'offer', 'hired', 'rejected', 'no_response', 'withdrawn')),
    -- NULL only when the date is genuinely unknown (e.g. an undated seed application).
    status_date    TEXT    CHECK (status_date IS NULL OR date(status_date) IS status_date),
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_status_history_application ON status_history (application_id, id);

CREATE TABLE activities (
    id              INTEGER PRIMARY KEY,
    date            TEXT    CHECK (date IS NULL OR date(date) IS date),
    date_precision  TEXT    NOT NULL CHECK (date_precision IN ('exact', 'week', 'unknown')),
    week_of         TEXT    CHECK (week_of IS NULL OR
                                (date(week_of) IS week_of AND strftime('%w', week_of) = '1')),
    type            TEXT    NOT NULL CHECK (type IN
                        ('recruiter_contact', 'networking', 'agency_registration', 'job_fair',
                         'workshop', 'course', 'other')),
    description     TEXT    NOT NULL CHECK (length(trim(description)) > 0),
    related_company TEXT,
    voided          INTEGER NOT NULL DEFAULT 0 CHECK (voided IN (0, 1)),
    void_reason     TEXT,
    voided_at       TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (
        (date_precision = 'exact'   AND date IS NOT NULL AND week_of IS NULL) OR
        (date_precision = 'week'    AND date IS NULL     AND week_of IS NOT NULL) OR
        (date_precision = 'unknown' AND date IS NULL     AND week_of IS NULL)
    ),
    CHECK (
        (voided = 0 AND void_reason IS NULL AND voided_at IS NULL) OR
        (voided = 1 AND length(trim(void_reason)) > 0 AND voided_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX ux_activities_natural
    ON activities (type, description, coalesce(date, ''), coalesce(week_of, ''));

-- Audit log: one row per changed column per UPDATE, written by triggers below.
CREATE TABLE edits (
    id          INTEGER PRIMARY KEY,
    table_name  TEXT    NOT NULL,
    row_id      INTEGER NOT NULL,
    column_name TEXT    NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    edited_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX ix_edits_row ON edits (table_name, row_id);

-- Retention: no deletes, anywhere.
CREATE TRIGGER no_delete_claim BEFORE DELETE ON claim
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_applications BEFORE DELETE ON applications
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_status_history BEFORE DELETE ON status_history
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_activities BEFORE DELETE ON activities
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_edits BEFORE DELETE ON edits
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;

-- Append-only tables: add a new row instead of editing an old one.
CREATE TRIGGER no_update_status_history BEFORE UPDATE ON status_history
BEGIN SELECT RAISE(ABORT, 'status_history is append-only: add a new status row instead'); END;
CREATE TRIGGER no_update_edits BEFORE UPDATE ON edits
BEGIN SELECT RAISE(ABORT, 'edits is an append-only audit log'); END;

-- The claim row: label, start and creation time are fixed; it freezes once sealed.
CREATE TRIGGER fixed_identity_claim BEFORE UPDATE OF id, label, start_date, created_at ON claim
BEGIN SELECT RAISE(ABORT, 'a claim''s label and start date can not be changed'); END;
CREATE TRIGGER frozen_sealed_claim BEFORE UPDATE ON claim WHEN OLD.sealed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
-- An end date can't cut off records that are already in the file.
CREATE TRIGGER claim_end_covers_records BEFORE UPDATE OF end_date ON claim
WHEN NEW.end_date IS NOT NULL AND (
    EXISTS (SELECT 1 FROM applications
            WHERE (date_precision = 'exact' AND date_applied > NEW.end_date)
               OR (date_precision = 'week' AND week_of > NEW.end_date))
    OR EXISTS (SELECT 1 FROM activities
               WHERE (date_precision = 'exact' AND date > NEW.end_date)
                  OR (date_precision = 'week' AND week_of > NEW.end_date)))
BEGIN SELECT RAISE(ABORT, 'the claim has records after that end date'); END;

-- Sealed: nothing may be added or changed.
CREATE TRIGGER sealed_insert_applications BEFORE INSERT ON applications
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER sealed_update_applications BEFORE UPDATE ON applications
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER sealed_insert_status_history BEFORE INSERT ON status_history
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER sealed_insert_activities BEFORE INSERT ON activities
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;
CREATE TRIGGER sealed_update_activities BEFORE UPDATE ON activities
WHEN (SELECT sealed_at FROM claim) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'this claim is sealed: its record can no longer change'); END;

-- Records belong to the claim period. The app checks this first with a friendlier
-- message; these triggers are the second line. Unknown dates are always allowed.
CREATE TRIGGER claim_required_applications BEFORE INSERT ON applications
WHEN NOT EXISTS (SELECT 1 FROM claim)
BEGIN SELECT RAISE(ABORT, 'this file has no claim row'); END;
CREATE TRIGGER claim_required_activities BEFORE INSERT ON activities
WHEN NOT EXISTS (SELECT 1 FROM claim)
BEGIN SELECT RAISE(ABORT, 'this file has no claim row'); END;
CREATE TRIGGER claim_period_insert_applications BEFORE INSERT ON applications
WHEN (NEW.date_precision = 'exact' AND (
          NEW.date_applied < (SELECT start_date FROM claim)
          OR NEW.date_applied > coalesce((SELECT end_date FROM claim), '9999-12-31')))
  OR (NEW.date_precision = 'week' AND (
          date(NEW.week_of, '+6 days') < (SELECT start_date FROM claim)
          OR NEW.week_of > coalesce((SELECT end_date FROM claim), '9999-12-31')))
BEGIN SELECT RAISE(ABORT, 'the application date is outside the claim period'); END;
CREATE TRIGGER claim_period_update_applications
BEFORE UPDATE OF date_applied, date_precision, week_of ON applications
WHEN (NEW.date_precision = 'exact' AND (
          NEW.date_applied < (SELECT start_date FROM claim)
          OR NEW.date_applied > coalesce((SELECT end_date FROM claim), '9999-12-31')))
  OR (NEW.date_precision = 'week' AND (
          date(NEW.week_of, '+6 days') < (SELECT start_date FROM claim)
          OR NEW.week_of > coalesce((SELECT end_date FROM claim), '9999-12-31')))
BEGIN SELECT RAISE(ABORT, 'the application date is outside the claim period'); END;
CREATE TRIGGER claim_period_insert_activities BEFORE INSERT ON activities
WHEN (NEW.date_precision = 'exact' AND (
          NEW.date < (SELECT start_date FROM claim)
          OR NEW.date > coalesce((SELECT end_date FROM claim), '9999-12-31')))
  OR (NEW.date_precision = 'week' AND (
          date(NEW.week_of, '+6 days') < (SELECT start_date FROM claim)
          OR NEW.week_of > coalesce((SELECT end_date FROM claim), '9999-12-31')))
BEGIN SELECT RAISE(ABORT, 'the activity date is outside the claim period'); END;
CREATE TRIGGER claim_period_update_activities
BEFORE UPDATE OF date, date_precision, week_of ON activities
WHEN (NEW.date_precision = 'exact' AND (
          NEW.date < (SELECT start_date FROM claim)
          OR NEW.date > coalesce((SELECT end_date FROM claim), '9999-12-31')))
  OR (NEW.date_precision = 'week' AND (
          date(NEW.week_of, '+6 days') < (SELECT start_date FROM claim)
          OR NEW.week_of > coalesce((SELECT end_date FROM claim), '9999-12-31')))
BEGIN SELECT RAISE(ABORT, 'the activity date is outside the claim period'); END;

-- Voided rows are frozen, which also means a void can't be undone.
CREATE TRIGGER frozen_voided_applications BEFORE UPDATE ON applications WHEN OLD.voided = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;
CREATE TRIGGER frozen_voided_activities BEFORE UPDATE ON activities WHEN OLD.voided = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;

-- Identity, the link to search.db, and creation time never change.
CREATE TRIGGER fixed_identity_applications
BEFORE UPDATE OF id, correlation_id, created_at ON applications
BEGIN SELECT RAISE(ABORT, 'id, correlation_id and created_at can not be changed'); END;
CREATE TRIGGER fixed_identity_activities BEFORE UPDATE OF id, created_at ON activities
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;

-- Audit triggers: record every changed column of every permitted UPDATE.
CREATE TRIGGER audit_claim AFTER UPDATE ON claim
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'claim', NEW.id, 'end_date', OLD.end_date, NEW.end_date WHERE OLD.end_date IS NOT NEW.end_date
    UNION ALL SELECT 'claim', NEW.id, 'sealed_at', OLD.sealed_at, NEW.sealed_at WHERE OLD.sealed_at IS NOT NEW.sealed_at;
END;

CREATE TRIGGER audit_applications AFTER UPDATE ON applications
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'applications', NEW.id, 'employer', OLD.employer, NEW.employer WHERE OLD.employer IS NOT NEW.employer
    UNION ALL SELECT 'applications', NEW.id, 'role', OLD.role, NEW.role WHERE OLD.role IS NOT NEW.role
    UNION ALL SELECT 'applications', NEW.id, 'location', OLD.location, NEW.location WHERE OLD.location IS NOT NEW.location
    UNION ALL SELECT 'applications', NEW.id, 'work_model', OLD.work_model, NEW.work_model WHERE OLD.work_model IS NOT NEW.work_model
    UNION ALL SELECT 'applications', NEW.id, 'posting_url', OLD.posting_url, NEW.posting_url WHERE OLD.posting_url IS NOT NEW.posting_url
    UNION ALL SELECT 'applications', NEW.id, 'date_applied', OLD.date_applied, NEW.date_applied WHERE OLD.date_applied IS NOT NEW.date_applied
    UNION ALL SELECT 'applications', NEW.id, 'date_precision', OLD.date_precision, NEW.date_precision WHERE OLD.date_precision IS NOT NEW.date_precision
    UNION ALL SELECT 'applications', NEW.id, 'week_of', OLD.week_of, NEW.week_of WHERE OLD.week_of IS NOT NEW.week_of
    UNION ALL SELECT 'applications', NEW.id, 'method', OLD.method, NEW.method WHERE OLD.method IS NOT NEW.method
    UNION ALL SELECT 'applications', NEW.id, 'snapshot_path', OLD.snapshot_path, NEW.snapshot_path WHERE OLD.snapshot_path IS NOT NEW.snapshot_path
    UNION ALL SELECT 'applications', NEW.id, 'resume_file', OLD.resume_file, NEW.resume_file WHERE OLD.resume_file IS NOT NEW.resume_file
    UNION ALL SELECT 'applications', NEW.id, 'voided', OLD.voided, NEW.voided WHERE OLD.voided IS NOT NEW.voided
    UNION ALL SELECT 'applications', NEW.id, 'void_reason', OLD.void_reason, NEW.void_reason WHERE OLD.void_reason IS NOT NEW.void_reason
    UNION ALL SELECT 'applications', NEW.id, 'voided_at', OLD.voided_at, NEW.voided_at WHERE OLD.voided_at IS NOT NEW.voided_at;
END;

CREATE TRIGGER audit_activities AFTER UPDATE ON activities
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'activities', NEW.id, 'date', OLD.date, NEW.date WHERE OLD.date IS NOT NEW.date
    UNION ALL SELECT 'activities', NEW.id, 'date_precision', OLD.date_precision, NEW.date_precision WHERE OLD.date_precision IS NOT NEW.date_precision
    UNION ALL SELECT 'activities', NEW.id, 'week_of', OLD.week_of, NEW.week_of WHERE OLD.week_of IS NOT NEW.week_of
    UNION ALL SELECT 'activities', NEW.id, 'type', OLD.type, NEW.type WHERE OLD.type IS NOT NEW.type
    UNION ALL SELECT 'activities', NEW.id, 'description', OLD.description, NEW.description WHERE OLD.description IS NOT NEW.description
    UNION ALL SELECT 'activities', NEW.id, 'related_company', OLD.related_company, NEW.related_company WHERE OLD.related_company IS NOT NEW.related_company
    UNION ALL SELECT 'activities', NEW.id, 'voided', OLD.voided, NEW.voided WHERE OLD.voided IS NOT NEW.voided
    UNION ALL SELECT 'activities', NEW.id, 'void_reason', OLD.void_reason, NEW.void_reason WHERE OLD.void_reason IS NOT NEW.void_reason
    UNION ALL SELECT 'activities', NEW.id, 'voided_at', OLD.voided_at, NEW.voided_at WHERE OLD.voided_at IS NOT NEW.voided_at;
END;
