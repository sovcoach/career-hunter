-- 0006_discovery_notified (search track): when a pending discovery row was sent.
--
-- The bot sets this after Telegram accepts the message. Null means it has not
-- been sent. A send that fails leaves it null so the next poll can try again.
-- EI files are not changed by this migration.

ALTER TABLE discovery_candidates ADD COLUMN notified_at TEXT
    CHECK (notified_at IS NULL OR notified_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
