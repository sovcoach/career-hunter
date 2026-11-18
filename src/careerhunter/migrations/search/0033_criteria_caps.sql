-- 0033_criteria_caps (search track): criteria rules have a kind, veto or cap (ADR-028).
--
-- Each rule in the criteria file now starts with its kind: `veto:` or `cap low:`,
-- `cap medium:` or `cap high:`. A rule without a valid kind holds scoring like any
-- unusable file, so `component_health.last_failure` gains `no-kind`. SQLite can't
-- change a CHECK in place, so the table is rebuilt with the same columns and every
-- row copied as it was. No path and no file text is stored.
-- EI files are not changed by this migration.

CREATE TABLE component_health_new (
    component        TEXT    PRIMARY KEY CHECK (component IN
                         ('lm_studio', 'searxng', 'boards', 'criteria')),
    failures         INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure     TEXT    CHECK (last_failure IS NULL OR last_failure IN
                         ('unreachable', 'unauthorized', 'bad-reply', 'throttled', 'failed',
                          'outside', 'missing', 'unreadable', 'loose', 'not-utf8',
                          'too-long', 'empty', 'not-numbered', 'out-of-order',
                          'rule-too-long', 'too-many-rules', 'no-kind')),
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
