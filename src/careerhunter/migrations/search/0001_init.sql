-- 0001_init (search track): the search and learning layer, search.db.
--
-- This file holds everything career-hunter learns from: every posting surfaced by any
-- source, the identity used to recognize it again, fit ratings and gaps, LLM output,
-- scout metadata, the decision to apply or skip (with the skip reason), and the full
-- decision history, so later phases can learn what fits from real outcomes.
--
-- None of this belongs in the EI record, which lives in its own file per claim (see
-- ei_record/0001_init.sql). An application here is linked to its EI counterpart only by
-- an opaque, random correlation_id, plus the label of the claim it was recorded under
-- (NULL when no claim was active or it fell outside the claim).
--
-- This layer is editable and has no retention triggers, so a later retention policy can
-- compact it. The one fixed thing is the link: correlation_id never changes, and
-- ei_claim can be set once and never changed after that.
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
    source       TEXT    NOT NULL CHECK (source IN
                     ('grok', 'watcher', 'discovery', 'manual', 'self_sourced')),
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
    source         TEXT    NOT NULL CHECK (source IN
                       ('grok', 'watcher', 'discovery', 'manual', 'self_sourced')),
    fit_rating     INTEGER CHECK (fit_rating IS NULL OR fit_rating BETWEEN 0 AND 10),
    gaps           TEXT,
    llm_output     TEXT,
    scout_metadata TEXT    CHECK (scout_metadata IS NULL OR json_valid(scout_metadata)),
    -- Relative to the data directory, so references stay valid if the drive is renamed.
    snapshot_path  TEXT    CHECK (snapshot_path IS NULL OR substr(snapshot_path, 1, 1) <> '/'),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (company_id, requisition_id)
);
CREATE UNIQUE INDEX ux_postings_fallback
    ON postings (company_id, title, location, substr(first_seen, 1, 7))
    WHERE url IS NULL AND requisition_id IS NULL;
CREATE INDEX ix_postings_company ON postings (company_id);

-- The decision to apply. One application per posting per date is the natural key, so
-- re-applying to the same role on another date is a new row.
CREATE TABLE applications (
    id             INTEGER PRIMARY KEY,
    correlation_id TEXT    NOT NULL UNIQUE CHECK (
                       length(correlation_id) = 32 AND correlation_id NOT GLOB '*[^0-9a-f]*'),
    ei_claim       TEXT    CHECK (ei_claim IS NULL OR
                                  ei_claim GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
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
    reasoning      TEXT,
    notes          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (
        (date_precision = 'exact'   AND date_applied IS NOT NULL AND week_of IS NULL) OR
        (date_precision = 'week'    AND date_applied IS NULL     AND week_of IS NOT NULL) OR
        (date_precision = 'unknown' AND date_applied IS NULL     AND week_of IS NULL)
    )
);
CREATE UNIQUE INDEX ux_applications_natural
    ON applications (posting_id, date_precision, coalesce(date_applied, ''), coalesce(week_of, ''));
CREATE INDEX ix_applications_claim ON applications (ei_claim);

-- Every status the application has been through, in order: the training signal.
CREATE TABLE decision_history (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications (id),
    status         TEXT    NOT NULL CHECK (status IN
                       ('applied', 'screening', 'assessment_submitted', 'interview_scheduled',
                        'offer', 'hired', 'rejected', 'no_response', 'withdrawn')),
    status_date    TEXT    CHECK (status_date IS NULL OR date(status_date) IS status_date),
    note           TEXT,
    recorded_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_decision_history_application ON decision_history (application_id, id);

-- The decision not to apply, with the reason. Skips never reach the EI record.
CREATE TABLE skips (
    id         INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings (id),
    date       TEXT    NOT NULL CHECK (date(date) IS date),
    reason     TEXT    NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_skips_posting ON skips (posting_id);

-- The link to the EI record.
CREATE TRIGGER fixed_correlation_id BEFORE UPDATE OF correlation_id ON applications
WHEN OLD.correlation_id IS NOT NEW.correlation_id
BEGIN SELECT RAISE(ABORT, 'correlation_id can not be changed'); END;
CREATE TRIGGER ei_claim_set_once BEFORE UPDATE OF ei_claim ON applications
WHEN OLD.ei_claim IS NOT NULL AND OLD.ei_claim IS NOT NEW.ei_claim
BEGIN SELECT RAISE(ABORT, 'ei_claim can be set once and not changed'); END;
