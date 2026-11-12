-- 0023_score_parse_failures (search track): replies from the model that did not parse.
--
-- One row per watcher or discovery posting whose scoring reply did not parse (a reply
-- with no usable JSON, or a reasoning model's thinking that never closed). `failures`
-- counts them, and `last_failed_at` is the last one. After a fixed number of failures
-- (`watch.PARSE_FAILURES_TO_GIVE_UP`) the watcher gives up: `given_up_at` is set, the
-- posting is never sent to the model again, it is not counted as held for scoring, and
-- `doctor` counts it beside the bot's refusals. It stays unscored and unskipped. The row
-- is removed once a reply for the posting parses. A model that does not answer is not a
-- parse failure and is not counted here.
-- EI files are not changed by this migration.

CREATE TABLE score_parse_failures (
    posting_id     INTEGER PRIMARY KEY REFERENCES postings (id),
    failures       INTEGER NOT NULL CHECK (failures >= 1),
    last_failed_at TEXT    NOT NULL CHECK (last_failed_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    given_up_at    TEXT    CHECK (given_up_at IS NULL OR given_up_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);
