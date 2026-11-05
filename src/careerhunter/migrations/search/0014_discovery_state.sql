-- 0014_discovery_state (search track): where discovery's query rotation is, and its backoff.
--
-- One row. `cursor` is the index of the next query in the full list of discovery
-- queries (titles by lane priority, every variant, times places, times hosts); a run
-- takes the next `discovery.queries_per_run` from there and moves it on, wrapping
-- around. The list is rebuilt from config each run, so a cursor past its end wraps.
-- `last_pass_at` is when the last discovery pass was claimed, so passes inside the
-- watch run keep `discovery.interval_minutes` apart. `failures` counts failed passes in
-- a row, and `backoff_until` is when the next pass may start after one; a pass that
-- succeeds sets both back. No query, result or response is stored here.
-- EI files are not changed by this migration.

CREATE TABLE discovery_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    cursor        INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
    last_pass_at  TEXT    CHECK (last_pass_at IS NULL OR last_pass_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    failures      INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    backoff_until TEXT    CHECK (backoff_until IS NULL OR backoff_until GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((failures = 0) = (backoff_until IS NULL))
);

INSERT INTO discovery_state (id) VALUES (1);
