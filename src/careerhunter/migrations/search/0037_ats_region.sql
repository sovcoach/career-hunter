-- 0037_ats_region (search track): a Lever board's region.
--
-- Lever serves its EU accounts from their own hosts (api.eu.lever.co for the API and
-- jobs.eu.lever.co for the board and posting pages), so a Lever board has to say which
-- region it is in. `ats_region` on `companies` and on `discovery_candidates` is NULL for
-- every board on the global hosts, and 'eu' only for a Lever board in the EU region.
-- Discovery sets it for a board found on jobs.eu.lever.co, and a Lever watchlist entry
-- sets it with `ats_region: eu`. It is not part of a board's key, so no posting key
-- changes. Every existing row keeps NULL.
-- EI files are not changed by this migration.

ALTER TABLE companies ADD COLUMN ats_region TEXT
    CHECK (ats_region IS NULL OR (ats_region = 'eu' AND ats_type = 'lever'));

ALTER TABLE discovery_candidates ADD COLUMN ats_region TEXT
    CHECK (ats_region IS NULL OR (ats_region = 'eu' AND ats_type = 'lever'));
