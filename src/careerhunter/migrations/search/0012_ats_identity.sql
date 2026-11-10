-- 0012_ats_identity (search track): Workday's identity, and a wider discovery queue (#35, #41).
--
-- A Workday board is watched per employer: its tenant (`ats_slug`), its `wdN` host
-- (`ats_host`, such as wd5) and its site (`ats_site`). `companies` gets the two new
-- columns. Both are null for every other ATS, and a Workday company stored before this
-- migration keeps them null until the watcher fills them in.
--
-- `discovery_candidates` is rebuilt so its CHECKs can widen: a board may be any of the
-- six ATS types the watcher reads, a Workday board carries its host and site, and the
-- status `gone` marks a board whose confirming board-API request said it does not
-- exist, so it is never queued again. `career` rows stay valid for the rows already
-- stored; discovery no longer queues new ones. Every row, id and notice stamp is copied.
--
-- Foreign keys. The migration runs inside one BEGIN IMMEDIATE transaction, where
-- `PRAGMA foreign_keys` is a no-op, so the rebuild is built not to need it: no table
-- references `discovery_candidates` and it references none, so creating the new table,
-- copying, dropping the old one and renaming the new one never touches a foreign key,
-- and enforcement stays on throughout. The indexes are dropped with the old table and
-- made again on the new one.
-- EI files are not changed by this migration.

ALTER TABLE companies ADD COLUMN ats_host TEXT
    CHECK (ats_host IS NULL OR (
        ats_type = 'workday' AND ats_host GLOB 'wd[0-9]*' AND ats_host NOT GLOB 'wd*[^0-9]*'));

ALTER TABLE companies ADD COLUMN ats_site TEXT
    CHECK (ats_site IS NULL OR (
        ats_type = 'workday' AND ats_host IS NOT NULL AND length(trim(ats_site)) > 0));

CREATE TABLE discovery_candidates_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT    NOT NULL CHECK (kind IN ('board', 'career')),
    url            TEXT    NOT NULL CHECK (length(trim(url)) > 0),
    ats_type       TEXT,
    ats_slug       TEXT,
    ats_host       TEXT,
    ats_site       TEXT,
    suggested_name TEXT    NOT NULL CHECK (length(trim(suggested_name)) > 0),
    status         TEXT    NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'gone')),
    first_seen     TEXT    NOT NULL CHECK (date(first_seen) IS NOT NULL),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    notified_at    TEXT    CHECK (notified_at IS NULL OR notified_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (
        (kind = 'board'
            AND ats_type IN ('greenhouse', 'lever', 'ashby', 'smartrecruiters', 'bamboohr',
                             'workday')
            AND ats_slug IS NOT NULL
            AND length(trim(ats_slug)) > 0
            AND (
                (ats_type = 'workday'
                    AND ats_host GLOB 'wd[0-9]*' AND ats_host NOT GLOB 'wd*[^0-9]*'
                    AND ats_site IS NOT NULL AND length(trim(ats_site)) > 0)
                OR
                (ats_type <> 'workday' AND ats_host IS NULL AND ats_site IS NULL)
            ))
        OR
        (kind = 'career' AND status <> 'gone'
            AND ats_type IS NULL AND ats_slug IS NULL AND ats_host IS NULL AND ats_site IS NULL)
    )
);

INSERT INTO discovery_candidates_new
    (id, kind, url, ats_type, ats_slug, ats_host, ats_site, suggested_name, status,
     first_seen, created_at, notified_at)
SELECT id, kind, url, ats_type, ats_slug, NULL, NULL, suggested_name, status,
       first_seen, created_at, notified_at
FROM discovery_candidates;

DROP TABLE discovery_candidates;

ALTER TABLE discovery_candidates_new RENAME TO discovery_candidates;

CREATE UNIQUE INDEX ux_discovery_board
    ON discovery_candidates (ats_type, ats_slug, coalesce(ats_host, ''), coalesce(ats_site, ''))
    WHERE kind = 'board';

CREATE UNIQUE INDEX ux_discovery_career_url
    ON discovery_candidates (url)
    WHERE kind = 'career';
