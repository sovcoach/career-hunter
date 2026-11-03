-- 0005_discovery_candidates (search track): a queue of companies found by discovery.
--
-- A row is a board or a career page, not a posting. The result title, the snippet,
-- and the SearXNG response are not stored. EI files are not changed by this migration.

CREATE TABLE discovery_candidates (
    id             INTEGER PRIMARY KEY,
    kind           TEXT    NOT NULL CHECK (kind IN ('board', 'career')),
    url            TEXT    NOT NULL CHECK (length(trim(url)) > 0),
    ats_type       TEXT,
    ats_slug       TEXT,
    suggested_name TEXT    NOT NULL CHECK (length(trim(suggested_name)) > 0),
    status         TEXT    NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    first_seen     TEXT    NOT NULL CHECK (date(first_seen) IS NOT NULL),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (
        (kind = 'board'
            AND ats_type IN ('greenhouse', 'lever', 'ashby')
            AND ats_slug IS NOT NULL
            AND length(trim(ats_slug)) > 0)
        OR
        (kind = 'career' AND ats_type IS NULL AND ats_slug IS NULL)
    )
);

CREATE UNIQUE INDEX ux_discovery_board
    ON discovery_candidates (ats_type, ats_slug)
    WHERE kind = 'board';

CREATE UNIQUE INDEX ux_discovery_career_url
    ON discovery_candidates (url)
    WHERE kind = 'career';
