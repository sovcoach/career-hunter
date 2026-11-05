-- 0009_fit_and_watch_runs (search track): each posting's fit, and a row per watch run.
--
-- `postings.fit` is the fit of the gap tier a posting was scored into: low, medium or
-- high, from that tier's `fit` key in config. It is null until the posting is scored
-- and the tier's name is found in config. The bot alerts only postings with a fit.
-- `fit_rating` (0-10) is a different column and stays null.
--
-- `watch_runs` gets one row when a watch run starts. The row is finished at the end of
-- the run with its board counts and the postings still held. A run that crashes, is
-- killed or aborts keeps `finished_at` null and every count null, so it is never read
-- as a success or as zero held. `budget_reached` is 1 when the run's time budget left
-- something for the next run; such a run still finishes.
-- EI files are not changed by this migration.

ALTER TABLE postings ADD COLUMN fit TEXT
    CHECK (fit IS NULL OR fit IN ('low', 'medium', 'high'));

CREATE TABLE watch_runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT    NOT NULL CHECK (started_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    finished_at    TEXT    CHECK (finished_at IS NULL OR (finished_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                       AND finished_at >= started_at)),
    budget_reached INTEGER NOT NULL DEFAULT 0 CHECK (budget_reached IN (0, 1)),
    boards_fetched INTEGER CHECK (boards_fetched IS NULL OR boards_fetched >= 0),
    boards_failed  INTEGER CHECK (boards_failed IS NULL OR boards_failed >= 0),
    boards_left    INTEGER CHECK (boards_left IS NULL OR boards_left >= 0),
    held_unscored  INTEGER CHECK (held_unscored IS NULL OR held_unscored >= 0),
    held_no_fit    INTEGER CHECK (held_no_fit IS NULL OR held_no_fit >= 0),
    held_off_lane  INTEGER CHECK (held_off_lane IS NULL OR held_off_lane >= 0),
    -- Unfinished: no counts and no budget flag. Finished: every count is set.
    CHECK (
        (finished_at IS NULL AND budget_reached = 0
            AND boards_fetched IS NULL AND boards_failed IS NULL AND boards_left IS NULL
            AND held_unscored IS NULL AND held_no_fit IS NULL AND held_off_lane IS NULL)
        OR
        (finished_at IS NOT NULL
            AND boards_fetched IS NOT NULL AND boards_failed IS NOT NULL
            AND boards_left IS NOT NULL AND held_unscored IS NOT NULL
            AND held_no_fit IS NOT NULL AND held_off_lane IS NOT NULL)
    )
);
