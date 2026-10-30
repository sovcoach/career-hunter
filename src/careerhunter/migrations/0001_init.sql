-- 0001_init: the EI job-search record.
--
-- Retention: Service Canada may request this record and it must be kept for 6 years.
-- Nothing in this schema cascades, and every table refuses DELETE outright.
--
-- Corrections without losing history:
--   * applications and activities are never deleted; a mistaken one is VOIDED
--     (voided = 1 with a void_reason). Voided rows stay in the database, are left out of
--     exports, and are frozen: once voided, a row can't be changed again.
--   * every permitted UPDATE on companies, postings, applications, activities and skips
--     is recorded column-by-column in `edits` by the audit triggers at the end of this file.
--     `edits` itself is append-only.
--   * status_history is append-only: status changes are new rows.
--
-- Dates are ISO-8601 text: 'YYYY-MM-DD' for dates, 'YYYY-MM-DDTHH:MM:SSZ' for timestamps.

CREATE TABLE companies (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) > 0),
    ats_type     TEXT    CHECK (ats_type IS NULL OR ats_type IN
                     ('greenhouse', 'lever', 'ashby', 'workday',
                      'successfactors', 'icims', 'taleo', 'smartrecruiters',
                      'jobvite', 'bamboohr', 'other')),
    ats_slug     TEXT,
    careers_url  TEXT,
    watchlist    INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
    source       TEXT    NOT NULL CHECK (source IN ('grok', 'watcher', 'discovery', 'manual')),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Posting identity, strongest first:
--   1. url                         (unique when present)
--   2. company + requisition_id    (unique when present)
--   3. company + title + location + month first seen, only when neither of the above is
--      known, so a re-post of the same role in a later month is a new posting.
CREATE TABLE postings (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies (id),
    title          TEXT    NOT NULL COLLATE NOCASE CHECK (length(trim(title)) > 0),
    location       TEXT    NOT NULL COLLATE NOCASE DEFAULT '',
    work_model     TEXT    CHECK (work_model IS NULL OR work_model IN ('onsite', 'hybrid', 'remote')),
    schedule       TEXT,
    url            TEXT    UNIQUE,
    requisition_id TEXT    CHECK (requisition_id IS NULL OR length(trim(requisition_id)) > 0),
    first_seen     TEXT    NOT NULL CHECK (date(first_seen) IS NOT NULL),
    source         TEXT    NOT NULL CHECK (source IN ('grok', 'watcher', 'discovery', 'manual')),
    fit_rating     INTEGER CHECK (fit_rating IS NULL OR fit_rating BETWEEN 0 AND 10),
    gaps           TEXT,
    -- Relative to the data directory, so the record stays valid if the drive is renamed.
    snapshot_path  TEXT    CHECK (snapshot_path IS NULL OR substr(snapshot_path, 1, 1) <> '/'),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, requisition_id)
);
CREATE UNIQUE INDEX ux_postings_fallback
    ON postings (company_id, title, location, substr(first_seen, 1, 7))
    WHERE url IS NULL AND requisition_id IS NULL;
CREATE INDEX ix_postings_company ON postings (company_id);

-- Several applications may point at one posting (re-applying to the same role on a
-- different date). One application per posting per date is the idempotency key.
CREATE TABLE applications (
    id             INTEGER PRIMARY KEY,
    posting_id     INTEGER NOT NULL REFERENCES postings (id),
    date_applied   TEXT    CHECK (date_applied IS NULL OR date(date_applied) IS date_applied),
    date_precision TEXT    NOT NULL CHECK (date_precision IN ('exact', 'week', 'unknown')),
    -- Always a Monday (strftime %w = 1).
    week_of        TEXT    CHECK (week_of IS NULL OR
                               (date(week_of) IS week_of AND strftime('%w', week_of) = '1')),
    method         TEXT    NOT NULL CHECK (method IN
                       ('company_site', 'linkedin_easy_apply', 'email', 'referral', 'agency',
                        'recruiter_inbound', 'other')),
    resume_file    TEXT,
    notes          TEXT,
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
    ON applications (posting_id, date_precision, coalesce(date_applied, ''), coalesce(week_of, ''));
CREATE INDEX ix_applications_dates ON applications (date_applied, week_of);

CREATE TABLE status_history (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('applied', 'screening', 'assessment', 'interview', 'offer',
                        'rejected', 'no_response', 'withdrawn')),
    -- NULL only when the date is genuinely unknown (e.g. an undated seed application).
    status_date    TEXT    CHECK (status_date IS NULL OR date(status_date) IS status_date),
    note           TEXT,
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

CREATE TABLE skips (
    id         INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings (id),
    date       TEXT    NOT NULL CHECK (date(date) IS date),
    reason     TEXT    NOT NULL CHECK (length(trim(reason)) > 0)
);
CREATE INDEX ix_skips_posting ON skips (posting_id);

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
CREATE TRIGGER no_delete_companies BEFORE DELETE ON companies
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_postings BEFORE DELETE ON postings
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_applications BEFORE DELETE ON applications
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_status_history BEFORE DELETE ON status_history
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_activities BEFORE DELETE ON activities
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_skips BEFORE DELETE ON skips
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;
CREATE TRIGGER no_delete_edits BEFORE DELETE ON edits
BEGIN SELECT RAISE(ABORT, 'deletes are disabled: EI records are kept for 6 years'); END;

-- Append-only tables: add a new row instead of editing an old one.
CREATE TRIGGER no_update_status_history BEFORE UPDATE ON status_history
BEGIN SELECT RAISE(ABORT, 'status_history is append-only: add a new status row instead'); END;
CREATE TRIGGER no_update_edits BEFORE UPDATE ON edits
BEGIN SELECT RAISE(ABORT, 'edits is an append-only audit log'); END;

-- Voided rows are frozen, which also means a void can't be undone.
CREATE TRIGGER frozen_voided_applications BEFORE UPDATE ON applications WHEN OLD.voided = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;
CREATE TRIGGER frozen_voided_activities BEFORE UPDATE ON activities WHEN OLD.voided = 1
BEGIN SELECT RAISE(ABORT, 'voided records are frozen'); END;

-- Identity and creation time never change.
CREATE TRIGGER fixed_identity_companies BEFORE UPDATE OF id, created_at ON companies
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;
CREATE TRIGGER fixed_identity_postings BEFORE UPDATE OF id, created_at ON postings
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;
CREATE TRIGGER fixed_identity_applications BEFORE UPDATE OF id, created_at ON applications
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;
CREATE TRIGGER fixed_identity_activities BEFORE UPDATE OF id, created_at ON activities
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;
CREATE TRIGGER fixed_identity_skips BEFORE UPDATE OF id ON skips
BEGIN SELECT RAISE(ABORT, 'id and created_at can not be changed'); END;

-- Audit triggers: record every changed column of every permitted UPDATE.
CREATE TRIGGER audit_companies AFTER UPDATE ON companies
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'companies', NEW.id, 'name', OLD.name, NEW.name WHERE OLD.name IS NOT NEW.name
    UNION ALL SELECT 'companies', NEW.id, 'ats_type', OLD.ats_type, NEW.ats_type WHERE OLD.ats_type IS NOT NEW.ats_type
    UNION ALL SELECT 'companies', NEW.id, 'ats_slug', OLD.ats_slug, NEW.ats_slug WHERE OLD.ats_slug IS NOT NEW.ats_slug
    UNION ALL SELECT 'companies', NEW.id, 'careers_url', OLD.careers_url, NEW.careers_url WHERE OLD.careers_url IS NOT NEW.careers_url
    UNION ALL SELECT 'companies', NEW.id, 'watchlist', OLD.watchlist, NEW.watchlist WHERE OLD.watchlist IS NOT NEW.watchlist
    UNION ALL SELECT 'companies', NEW.id, 'source', OLD.source, NEW.source WHERE OLD.source IS NOT NEW.source;
END;

CREATE TRIGGER audit_postings AFTER UPDATE ON postings
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'postings', NEW.id, 'company_id', OLD.company_id, NEW.company_id WHERE OLD.company_id IS NOT NEW.company_id
    UNION ALL SELECT 'postings', NEW.id, 'title', OLD.title, NEW.title WHERE OLD.title IS NOT NEW.title
    UNION ALL SELECT 'postings', NEW.id, 'location', OLD.location, NEW.location WHERE OLD.location IS NOT NEW.location
    UNION ALL SELECT 'postings', NEW.id, 'work_model', OLD.work_model, NEW.work_model WHERE OLD.work_model IS NOT NEW.work_model
    UNION ALL SELECT 'postings', NEW.id, 'schedule', OLD.schedule, NEW.schedule WHERE OLD.schedule IS NOT NEW.schedule
    UNION ALL SELECT 'postings', NEW.id, 'url', OLD.url, NEW.url WHERE OLD.url IS NOT NEW.url
    UNION ALL SELECT 'postings', NEW.id, 'requisition_id', OLD.requisition_id, NEW.requisition_id WHERE OLD.requisition_id IS NOT NEW.requisition_id
    UNION ALL SELECT 'postings', NEW.id, 'first_seen', OLD.first_seen, NEW.first_seen WHERE OLD.first_seen IS NOT NEW.first_seen
    UNION ALL SELECT 'postings', NEW.id, 'source', OLD.source, NEW.source WHERE OLD.source IS NOT NEW.source
    UNION ALL SELECT 'postings', NEW.id, 'fit_rating', OLD.fit_rating, NEW.fit_rating WHERE OLD.fit_rating IS NOT NEW.fit_rating
    UNION ALL SELECT 'postings', NEW.id, 'gaps', OLD.gaps, NEW.gaps WHERE OLD.gaps IS NOT NEW.gaps
    UNION ALL SELECT 'postings', NEW.id, 'snapshot_path', OLD.snapshot_path, NEW.snapshot_path WHERE OLD.snapshot_path IS NOT NEW.snapshot_path;
END;

CREATE TRIGGER audit_applications AFTER UPDATE ON applications
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'applications', NEW.id, 'posting_id', OLD.posting_id, NEW.posting_id WHERE OLD.posting_id IS NOT NEW.posting_id
    UNION ALL SELECT 'applications', NEW.id, 'date_applied', OLD.date_applied, NEW.date_applied WHERE OLD.date_applied IS NOT NEW.date_applied
    UNION ALL SELECT 'applications', NEW.id, 'date_precision', OLD.date_precision, NEW.date_precision WHERE OLD.date_precision IS NOT NEW.date_precision
    UNION ALL SELECT 'applications', NEW.id, 'week_of', OLD.week_of, NEW.week_of WHERE OLD.week_of IS NOT NEW.week_of
    UNION ALL SELECT 'applications', NEW.id, 'method', OLD.method, NEW.method WHERE OLD.method IS NOT NEW.method
    UNION ALL SELECT 'applications', NEW.id, 'resume_file', OLD.resume_file, NEW.resume_file WHERE OLD.resume_file IS NOT NEW.resume_file
    UNION ALL SELECT 'applications', NEW.id, 'notes', OLD.notes, NEW.notes WHERE OLD.notes IS NOT NEW.notes
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

CREATE TRIGGER audit_skips AFTER UPDATE ON skips
BEGIN
    INSERT INTO edits (table_name, row_id, column_name, old_value, new_value)
    SELECT 'skips', NEW.id, 'posting_id', OLD.posting_id, NEW.posting_id WHERE OLD.posting_id IS NOT NEW.posting_id
    UNION ALL SELECT 'skips', NEW.id, 'date', OLD.date, NEW.date WHERE OLD.date IS NOT NEW.date
    UNION ALL SELECT 'skips', NEW.id, 'reason', OLD.reason, NEW.reason WHERE OLD.reason IS NOT NEW.reason;
END;
