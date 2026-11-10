-- 0015_posting_ats_key (search track): a posting's identity on its ATS board.
--
-- `ats_key` is `<ats_type>:<board>:<posting id>` (board case-folded; a Workday board
-- is `<tenant>:<host>:<site>` and its posting id is its `/job/...` path). It is set
-- when the watcher stores or meets a posting it read from a board or a posting API,
-- and never changed. Discovery checks it before fetching a posting, so a posting is
-- known however its URL was found: a Greenhouse board on the company's own domain has
-- an `absolute_url` that is not the board URL. A posting stored before this migration
-- keeps it null until the watcher meets it again; the URL check still covers it.
-- EI files are not changed by this migration.

ALTER TABLE postings ADD COLUMN ats_key TEXT CHECK (ats_key IS NULL OR length(ats_key) > 0);

CREATE UNIQUE INDEX ux_postings_ats_key ON postings (ats_key) WHERE ats_key IS NOT NULL;
