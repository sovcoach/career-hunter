-- 0020_job_descriptions (search track): each application's job description, by
-- reference (ADR-024).
--
-- applications.jd_expected follows the EI rule: rows from before this migration get 0
-- (pre-feature), every writer from now on sets 1, and it never changes.
-- applications.jd_sha256 is the SHA-256 of the current JD's text, and jd_path its
-- snapshot file, relative to the data folder. search.db keeps no second copy of the
-- text: the snapshot file holds it, so an application that is only in search.db still
-- keeps its JD. This migration only adds columns and one trigger. EI files are not
-- changed by it.

ALTER TABLE applications
    ADD COLUMN jd_expected INTEGER NOT NULL DEFAULT 0 CHECK (jd_expected IN (0, 1));
ALTER TABLE applications
    ADD COLUMN jd_sha256 TEXT CHECK (jd_sha256 IS NULL OR (
        length(jd_sha256) = 64 AND jd_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE applications
    ADD COLUMN jd_path TEXT CHECK (jd_path IS NULL OR (
        jd_path GLOB 'snapshots/*' AND instr(jd_path, '..') = 0));

CREATE TRIGGER fixed_jd_expected_applications BEFORE UPDATE OF jd_expected ON applications
WHEN OLD.jd_expected IS NOT NEW.jd_expected
BEGIN SELECT RAISE(ABORT, 'jd_expected can not be changed'); END;
