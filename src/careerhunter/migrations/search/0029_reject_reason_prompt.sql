-- 0029_reject_reason_prompt (search track): the bot can ask why a candidate was rejected.
--
-- "Reject with reason…" on a discovery candidate records the rejection, then asks for
-- the reason as a reply to its question; the reply fills
-- `discovery_candidates.reject_reason` (search migration 0028). So `bot_prompts.kind`
-- gains `reject_reason`. SQLite can't change a CHECK in place, so the table is rebuilt
-- with the same columns and every row copied as it was.
-- EI files are not changed by this migration.

CREATE TABLE bot_prompts_new (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN
                   ('apply_date', 'activity_date', 'skip_reason', 'snapshot',
                    'reject_reason')),
    payload    TEXT    NOT NULL CHECK (json_valid(payload)),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT    NOT NULL,
    used_at    TEXT,
    PRIMARY KEY (chat_id, message_id)
);

INSERT INTO bot_prompts_new
    (chat_id, message_id, kind, payload, created_at, expires_at, used_at)
SELECT chat_id, message_id, kind, payload, created_at, expires_at, used_at
FROM bot_prompts;

DROP TABLE bot_prompts;

ALTER TABLE bot_prompts_new RENAME TO bot_prompts;
