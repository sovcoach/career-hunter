-- 0011_alert_digest (search track): key requirements and the scoring run on each posting.
--
-- `key_requirements` is a JSON array of one to three short phrases the local model
-- took from the posting itself, separate from the gap list. It is null when the model
-- gave none, when the posting was scored before this migration, or when every phrase
-- was dropped (too long, copied from the resume, or naming a gap). The code caps each
-- phrase; the CHECK keeps the shape. The bot's digest sends these phrases; gap names
-- are never sent.
--
-- `score_run_id` is the `watch_runs` row of the run that last gave the posting its
-- score or its fit. It is null for a posting scored before this migration, or outside a
-- watch run. The bot groups pending alerts by it, one digest per run.
-- EI files are not changed by this migration.

ALTER TABLE postings ADD COLUMN key_requirements TEXT
    CHECK (key_requirements IS NULL OR (
        json_valid(key_requirements)
        AND json_type(key_requirements) = 'array'
        AND json_array_length(key_requirements) BETWEEN 1 AND 3
    ));

ALTER TABLE postings ADD COLUMN score_run_id INTEGER REFERENCES watch_runs (id);
