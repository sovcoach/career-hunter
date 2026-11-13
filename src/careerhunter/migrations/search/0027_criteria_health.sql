-- 0027_criteria_health (search track): a criteria file that holds scoring is a component.
--
-- When `search.criteria_file` is set but can't be used, the watcher holds scoring for
-- the run, and the bot's health message says why. So `component_health` gains the
-- component `criteria`, and `last_failure` gains the criteria file's fixed reasons:
-- `outside`, `missing`, `unreadable`, `loose`, `not-utf8`, `too-long` and `empty`. No
-- path and no file text is stored. SQLite can't change a CHECK in place, so the table
-- is rebuilt with the same columns and every row copied as it was.
-- EI files are not changed by this migration.

CREATE TABLE component_health_new (
    component        TEXT    PRIMARY KEY CHECK (component IN
                         ('lm_studio', 'searxng', 'boards', 'criteria')),
    failures         INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure     TEXT    CHECK (last_failure IS NULL OR last_failure IN
                         ('unreachable', 'unauthorized', 'bad-reply', 'throttled', 'failed',
                          'outside', 'missing', 'unreadable', 'loose', 'not-utf8',
                          'too-long', 'empty')),
    held             INTEGER CHECK (held IS NULL OR held >= 0),
    updated_at       TEXT    CHECK (updated_at IS NULL OR updated_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    alerted_failures INTEGER CHECK (alerted_failures IS NULL OR alerted_failures >= 1),
    recovery_due     INTEGER NOT NULL DEFAULT 0 CHECK (recovery_due IN (0, 1)),
    CHECK ((failures = 0) = (last_failure IS NULL))
);

INSERT INTO component_health_new
    (component, failures, last_failure, held, updated_at, alerted_failures, recovery_due)
SELECT component, failures, last_failure, held, updated_at, alerted_failures, recovery_due
FROM component_health;

DROP TABLE component_health;

ALTER TABLE component_health_new RENAME TO component_health;

INSERT INTO component_health (component) VALUES ('criteria');
