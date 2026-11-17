-- 0028_discovery_postings (search track): a discovery candidate carries its postings.
--
-- A board is queued only once a posting it was found by is recorded with no skip, and
-- the bot sends a candidate only while one of its postings is still unskipped. So
-- `discovery_candidate_postings` links each candidate to those postings. A candidate
-- also keeps the company name the board's own reply gave (`display_name`), the
-- operator's optional reason for a rejection (`reject_reason`), and the day its
-- message was sent in the configured time zone (`notified_on`), which the daily cap
-- counts. Rows queued before this migration keep NULL in all three and have no links,
-- so they are not sent again. EI files are not changed by this migration.

CREATE TABLE discovery_candidate_postings (
    candidate_id INTEGER NOT NULL REFERENCES discovery_candidates (id),
    posting_id   INTEGER NOT NULL REFERENCES postings (id),
    PRIMARY KEY (candidate_id, posting_id)
);

ALTER TABLE discovery_candidates ADD COLUMN display_name TEXT
    CHECK (display_name IS NULL OR length(display_name) BETWEEN 1 AND 200);

ALTER TABLE discovery_candidates ADD COLUMN reject_reason TEXT
    CHECK (reject_reason IS NULL OR length(reject_reason) BETWEEN 1 AND 500);

ALTER TABLE discovery_candidates ADD COLUMN notified_on TEXT
    CHECK (notified_on IS NULL OR notified_on GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]');
