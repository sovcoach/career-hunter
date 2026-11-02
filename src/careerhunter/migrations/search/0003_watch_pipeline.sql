-- 0003_watch_pipeline (search track): when a posting alert was sent.
--
-- The bot sets this after Telegram accepts the message. Null means it has not been
-- sent. A send that fails leaves it null so the next poll can try again. Quiet hours
-- are not implemented; nothing here holds an alert back.

ALTER TABLE postings ADD COLUMN alerted_at TEXT
    CHECK (alerted_at IS NULL OR alerted_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
