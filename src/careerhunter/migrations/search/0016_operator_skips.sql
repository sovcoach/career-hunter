-- 0016_operator_skips (search track): which skips the operator made (#34).
--
-- `by_operator` is 1 for a skip the operator recorded (the bot's Skipped button), and 0
-- for one the watcher wrote (a filter, the posting age, a gap tier). Only an operator
-- skip suppresses a repost of the same role (`already_handled`); a filter skip does not,
-- so a changed config can still let a repost through.
--
-- Skips stored before this migration keep 0: the reason text can't tell the two apart
-- (`seniority` is both a button and a filter reason), so nothing is guessed. Only skips
-- recorded after it suppress reposts. `reason` stays free text.
-- EI files are not changed by this migration.

ALTER TABLE skips ADD COLUMN by_operator INTEGER NOT NULL DEFAULT 0
    CHECK (by_operator IN (0, 1));
