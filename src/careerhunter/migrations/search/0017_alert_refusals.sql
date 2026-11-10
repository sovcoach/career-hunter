-- 0017_alert_refusals (search track): Telegram refusals of an alert, kept across restarts (#42).
--
-- One row per watcher or discovery posting that Telegram refused when it was sent on
-- its own (HTTP 400 or 403: `TelegramRejected`). `refusals` counts them, and
-- `last_refused_at` is the last one. A refused posting goes behind every other pending
-- alert, so it can't take the same slot every poll. After a fixed number of refusals
-- (`bot.REFUSALS_TO_GIVE_UP`) the bot gives up: `given_up_at` is set, the posting is never
-- sent again by itself, and `doctor` counts it. It stays unalerted and unskipped, so it
-- is never counted as sent. The row is removed once Telegram accepts the posting.
-- A transport failure, a bad reply or HTTP 429 is not a refusal and is not counted here.
-- EI files are not changed by this migration.

CREATE TABLE alert_refusals (
    posting_id      INTEGER PRIMARY KEY REFERENCES postings (id),
    refusals        INTEGER NOT NULL CHECK (refusals >= 1),
    last_refused_at TEXT    NOT NULL CHECK (last_refused_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    given_up_at     TEXT    CHECK (given_up_at IS NULL OR given_up_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);
