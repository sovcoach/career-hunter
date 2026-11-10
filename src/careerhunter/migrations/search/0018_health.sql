-- 0018_health (search track): ran versus did useful work, and component outages (#42).
--
-- `watch_runs.useful` is set when a run finishes: 1 when at least one board was fetched
-- and parsed and, if postings were waiting to be scored, the scorer was reached; else 0.
-- An unfinished run has none. Runs finished before this migration keep null: unknown,
-- so doctor counts them neither as useful nor as unproductive.
--
-- `discovery_state.useful_at` is the last pass that got a usable reply to every query it
-- sent; `unproductive` counts the passes in a row that ran without one (a miss or a stop).
-- A pass that is off, inside its backoff, or skipped as busy did not run and changes
-- neither. The `discover.success` stamp in logs/ stays the "ran" stamp.
--
-- `component_health` has one row per component that can be out: the local model
-- (`lm_studio`, scoring), SearXNG (`searxng`), and the boards together (`boards`).
-- The watcher writes `failures` (runs in a row that failed it), the kind of the last
-- failure, and the postings held for scoring then. The bot writes what it has told the
-- operator: `alerted_failures` is the count named in the failure message it sent for
-- this outage, null when none was sent; `recovery_due` is 1 once the component worked
-- again after that message, until the recovery message is sent. So each outage gets one
-- failure message and one recovery message.
-- EI files are not changed by this migration.

ALTER TABLE watch_runs ADD COLUMN useful INTEGER
    CHECK (useful IS NULL OR (useful IN (0, 1) AND finished_at IS NOT NULL));

ALTER TABLE discovery_state ADD COLUMN useful_at TEXT
    CHECK (useful_at IS NULL OR useful_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
ALTER TABLE discovery_state ADD COLUMN unproductive INTEGER NOT NULL DEFAULT 0
    CHECK (unproductive >= 0);

CREATE TABLE component_health (
    component        TEXT    PRIMARY KEY CHECK (component IN ('lm_studio', 'searxng', 'boards')),
    failures         INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure     TEXT    CHECK (last_failure IS NULL OR
                         last_failure IN ('unreachable', 'bad-reply', 'throttled', 'failed')),
    held             INTEGER CHECK (held IS NULL OR held >= 0),
    updated_at       TEXT    CHECK (updated_at IS NULL OR updated_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    alerted_failures INTEGER CHECK (alerted_failures IS NULL OR alerted_failures >= 1),
    recovery_due     INTEGER NOT NULL DEFAULT 0 CHECK (recovery_due IN (0, 1)),
    CHECK ((failures = 0) = (last_failure IS NULL))
);
INSERT INTO component_health (component) VALUES ('lm_studio'), ('searxng'), ('boards');
