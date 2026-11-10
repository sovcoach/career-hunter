-- 0019_workday_posting_key (search track): a Workday posting's id is the trailing
-- `_<id>` of its path, not the whole `/job/...` path.
--
-- A Workday `ats_key` is now `workday:<tenant>:<host>:<site>:<id>`, where `<id>` is the
-- tail of the path's last segment after its last `_`, as it is (`JREQ205219-7` keeps
-- its -N suffix). The title and location slugs before it are left out, so a retitled
-- or moved posting keeps its key. A key stored in the old form (ending in the path)
-- is cleared here; the watcher sets the new one when it next meets the posting by its
-- URL, the same as for a posting stored before 0015. Only Workday keys are touched,
-- and a database with no Workday postings is unchanged. EI files are not changed by
-- this migration.

UPDATE postings SET ats_key = NULL WHERE ats_key LIKE 'workday:%:/job/%';
