-- 0038_score_call_draws (search track): the draws behind a voted scoring call.
--
-- With scoring.voting a posting is scored by two to three draws, and a draw that
-- doesn't parse or times out is replaced once, so one score_calls row can take several
-- draws' time. `duration_ms` stays the whole call. `draw_calls` is how many draws were
-- sent for it (replacements included), and `longest_draw_ms` the longest single draw,
-- which is what scoring.timeout_seconds limits. Both are NULL for a call scored by one
-- draw, a voted call that ended with no reply to count, and every existing row.
-- EI files are not changed by this migration.

ALTER TABLE score_calls ADD COLUMN draw_calls INTEGER
    CHECK (draw_calls IS NULL OR draw_calls BETWEEN 1 AND 6);

ALTER TABLE score_calls ADD COLUMN longest_draw_ms INTEGER
    CHECK (longest_draw_ms IS NULL OR longest_draw_ms >= 0);
