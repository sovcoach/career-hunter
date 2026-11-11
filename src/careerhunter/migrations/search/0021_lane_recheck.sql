-- 0021_lane_recheck (search track): a checked lane can move, and every move is logged.
--
-- 0010 set a posting's lane once. This drops that trigger so `careerhunter lanes
-- reassign` can move a lane that was already checked, against the current config and
-- matcher. The watcher still writes only rows with `lane_checked = 0`, so only an
-- explicit reassign moves a checked lane. `lane_checked` still can't go back from 1 to
-- 0. Every change of a checked posting's lane is written to `lane_changes` by a
-- trigger: the old lane and the new one, null for off-lane. The watcher's first check
-- of a posting is not a change and is not logged. EI files are not changed by this.

DROP TRIGGER posting_lane_set_once;

CREATE TRIGGER posting_lane_checked_stays BEFORE UPDATE OF lane_checked ON postings
WHEN OLD.lane_checked = 1 AND NEW.lane_checked IS NOT 1
BEGIN SELECT RAISE(ABORT, 'a checked posting''s lane_checked stays 1'); END;

CREATE TABLE lane_changes (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings (id),
    old_lane TEXT,
    new_lane TEXT,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX ix_lane_changes_posting ON lane_changes (posting_id);

CREATE TRIGGER posting_lane_change_logged AFTER UPDATE OF lane ON postings
WHEN OLD.lane_checked = 1 AND OLD.lane IS NOT NEW.lane
BEGIN
    INSERT INTO lane_changes (posting_id, old_lane, new_lane)
    VALUES (NEW.id, OLD.lane, NEW.lane);
END;
