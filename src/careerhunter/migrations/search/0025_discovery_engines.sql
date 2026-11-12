-- 0025_discovery_engines (search track): the search engines throttled in a discovery pass.
--
-- SearXNG asks several engines for each query and reports the ones that did not answer.
-- One engine suspended for a CAPTCHA or too many requests no longer stops a pass while
-- the others answer. `suspended_engines` is a JSON array of the names, lower case, of
-- the engines reported as throttled or suspended in the last pass that ran, and
-- `suspended_at` is when; both are null when none was. Engine names are SearXNG's own
-- (such as a public search engine's name), never a query, a result or a response.
-- EI files are not changed by this migration.

ALTER TABLE discovery_state ADD COLUMN suspended_engines TEXT
    CHECK (suspended_engines IS NULL OR (
        json_valid(suspended_engines) AND json_type(suspended_engines) = 'array'));
ALTER TABLE discovery_state ADD COLUMN suspended_at TEXT
    CHECK (suspended_at IS NULL OR suspended_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
