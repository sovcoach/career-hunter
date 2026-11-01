-- 0002_bot (search track): state for the Telegram bot (ADR-016).
--
-- Nothing a Telegram message says is ever used as a database identifier. Buttons carry
-- an opaque random token, and replies are matched to the bot's own prompt by message
-- id; both are looked up here and checked against the chat they were issued to.

-- One row per button. All buttons on one keyboard share `keyboard`, and using any one
-- of them uses up the whole keyboard.
CREATE TABLE bot_tokens (
    token      TEXT    PRIMARY KEY CHECK (
                   length(token) BETWEEN 16 AND 64 AND token NOT GLOB '*[^A-Za-z0-9_-]*'),
    keyboard   TEXT    NOT NULL,
    chat_id    INTEGER NOT NULL,
    -- The message carrying the button, recorded once it has been sent.
    message_id INTEGER,
    action     TEXT    NOT NULL,
    payload    TEXT    NOT NULL CHECK (json_valid(payload)),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT    NOT NULL,
    used_at    TEXT
);
CREATE INDEX ix_bot_tokens_keyboard ON bot_tokens (keyboard);

-- Short references ("a7") to applications, for /status. Scoped to one chat, and they
-- expire.
CREATE TABLE bot_refs (
    chat_id        INTEGER NOT NULL,
    ref            TEXT    NOT NULL CHECK (ref GLOB 'a[1-9]*' AND ref NOT GLOB 'a*[^0-9]*'),
    application_id INTEGER NOT NULL REFERENCES applications (id),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at     TEXT    NOT NULL,
    PRIMARY KEY (chat_id, ref)
);

-- A question the bot asked, answered by replying to that message: a date typed by
-- hand, a skip reason typed by hand, or the job ad sent as a file.
CREATE TABLE bot_prompts (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN
                   ('apply_date', 'activity_date', 'skip_reason', 'snapshot')),
    payload    TEXT    NOT NULL CHECK (json_valid(payload)),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT    NOT NULL,
    used_at    TEXT,
    PRIMARY KEY (chat_id, message_id)
);

-- Small values the bot keeps between runs, such as the Telegram update offset.
CREATE TABLE bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
