-- 0036_rereads_jd_duplicates_referral (search track): re-read failures, the kept JD's
-- hash, duplicate postings and the referral label.
--
-- `reread_failures` has one row per posting whose last re-reads of its board page
-- failed for a reason other than gone or throttled (HTTP 404, 410, 429 are not counted
-- here). `failures` counts them in a row, `reason` is the last one's fixed reason, and
-- `last_failed_at` is when. After `watch.REREADS_TO_SET_ASIDE` in a row the posting is
-- set aside (`set_aside_at`): it is not re-read again and not counted as gone until
-- `careerhunter rescore --set-aside` clears it. A re-read that succeeds removes the row.
-- No URL, status text or page text is stored.
--
-- `postings.jd_sha256` is the SHA-256 of the posting's kept job description after
-- normalizing (lower case, whitespace collapsed, long digit runs removed). It is set
-- with `snapshot_path` when the description is kept, and compared to find duplicates.
--
-- `postings.duplicate_of` points at the first posting of a group that is one role
-- posted more than once: the same company, title, location and job description. It
-- never points at the posting itself.
--
-- `postings.referral` is 1 for a posting the operator marked as a referral. Such a
-- posting counts as local and needs no location. It is 0 for every existing posting.
-- EI files are not changed by this migration.

CREATE TABLE reread_failures (
    posting_id     INTEGER PRIMARY KEY REFERENCES postings (id),
    failures       INTEGER NOT NULL CHECK (failures >= 1),
    reason         TEXT    NOT NULL CHECK (reason IN
                       ('http-403', 'http-4xx', 'http-5xx', 'fetch-error', 'parse')),
    last_failed_at TEXT    NOT NULL CHECK (last_failed_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    set_aside_at   TEXT    CHECK (set_aside_at IS NULL OR set_aside_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

ALTER TABLE postings ADD COLUMN jd_sha256 TEXT
    CHECK (jd_sha256 IS NULL OR (length(jd_sha256) = 64 AND jd_sha256 NOT GLOB '*[^0-9a-f]*'));

ALTER TABLE postings ADD COLUMN duplicate_of INTEGER REFERENCES postings (id)
    CHECK (duplicate_of IS NULL OR duplicate_of <> id);

ALTER TABLE postings ADD COLUMN referral INTEGER NOT NULL DEFAULT 0
    CHECK (referral IN (0, 1));

CREATE INDEX ix_postings_duplicate_of ON postings (duplicate_of) WHERE duplicate_of IS NOT NULL;
