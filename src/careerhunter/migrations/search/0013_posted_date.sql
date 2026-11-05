-- 0013_posted_date (search track): the date the ATS says a posting was posted (#36).
--
-- `posted_date` is the posting's own posted date, YYYY-MM-DD in UTC, as its board gave
-- it when the watcher stored it, or null when the board gave none (or none that could
-- be read). The posting age gate reads it, and `first_seen` when it is null, so the
-- recheck can skip a stored posting that has grown too old with the same rule. A
-- posting stored before this migration keeps it null.
-- EI files are not changed by this migration.

ALTER TABLE postings ADD COLUMN posted_date TEXT
    CHECK (posted_date IS NULL OR date(posted_date) IS posted_date);
