-- 0010_posting_lanes (search track): the title lane a posting matched, set once.
--
-- `lane` is the name of the best-priority lane in `search.lanes` whose title variants
-- appear in the posting's title, or null when none matched (off-lane). `lane_checked`
-- is 1 once the title has been checked against the lanes, and 0 before. A row with
-- `lane_checked` 0 has no lane. The watcher sets both when it stores a posting, skipped
-- ones included, and fills in rows still at 0 with `... WHERE lane_checked = 0`, so the
-- fill-in is safe to run twice. Once checked, neither column changes: a later change to
-- the lanes in config does not move a stored posting. With no `search.lanes`, nothing
-- is checked and every row stays at 0 until lanes are added.
-- EI files are not changed by this migration.

ALTER TABLE postings ADD COLUMN lane TEXT
    CHECK (lane IS NULL OR length(trim(lane)) > 0);

ALTER TABLE postings ADD COLUMN lane_checked INTEGER NOT NULL DEFAULT 0
    CHECK (lane_checked IN (0, 1) AND (lane_checked = 1 OR lane IS NULL));

CREATE TRIGGER posting_lane_set_once BEFORE UPDATE OF lane, lane_checked ON postings
WHEN OLD.lane_checked = 1 AND (NEW.lane_checked IS NOT 1 OR NEW.lane IS NOT OLD.lane)
BEGIN SELECT RAISE(ABORT, 'a posting''s lane is set once and not changed'); END;
