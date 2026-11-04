-- 0007_watch_board_attempts (search track): when each board was last attempted.
--
-- The watcher fetches boards it has never tried first, then the one tried longest
-- ago, so a run budget never leaves the same boards out every run. A row is written
-- just before each fetch, in its own transaction. A board the budget left out gets its
-- earlier value back, or loses its row if it had none. The key is "<ats_type>:<slug>"
-- with the slug case-folded. This table never touches companies or postings.
-- EI files are not changed by this migration.

CREATE TABLE watch_board_attempts (
    board_key       TEXT PRIMARY KEY CHECK (length(board_key) > 0),
    last_attempt_at TEXT NOT NULL CHECK (last_attempt_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);
