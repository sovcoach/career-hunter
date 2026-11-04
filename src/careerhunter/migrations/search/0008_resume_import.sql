-- 0008_resume_import (search track): which resume the scores were made with.
--
-- `careerhunter resume import` writes the resume text to resume/current.txt in the
-- data folder and records only its SHA-256 here, with the time. The text itself is
-- never stored in this database. The latest row is the resume in use.
--
-- A posting records the SHA-256 of the resume it was scored with, or null when it was
-- scored without one. When the resume changes, the watcher re-scores the postings
-- whose hash differs. `score_attempted_at` is when the posting was last sent to the
-- model, so a posting that keeps failing goes behind the others in the next run.
-- EI files are not changed by this migration.

CREATE TABLE resume_import (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT    NOT NULL CHECK (
                    length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    imported_at TEXT    NOT NULL CHECK (imported_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

ALTER TABLE postings ADD COLUMN scored_resume_sha256 TEXT
    CHECK (scored_resume_sha256 IS NULL OR (
        length(scored_resume_sha256) = 64 AND scored_resume_sha256 NOT GLOB '*[^0-9a-f]*'));

ALTER TABLE postings ADD COLUMN score_attempted_at TEXT
    CHECK (score_attempted_at IS NULL OR score_attempted_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
