-- 0005_requisitions (ei_record track): the requisitions one application covers.
--
-- When one role is posted more than once (the same company, title, location and job
-- description under different requisition numbers), career-hunter alerts it once and
-- an Applied tap records one application, not one per posting. `requisitions` names
-- every requisition that application covers, such as `R-1001, R-1002`, so the record
-- still says which postings it was for. It is set only when the application is
-- written, and only for such a group; every other application has none, and every
-- row from before this migration keeps NULL.
--
-- This migration only adds. It never UPDATEs a row, so a voided (frozen) application
-- doesn't stop it, and no audit trigger fires. A sealed claim's file is never migrated:
-- every reader handles a file still at an earlier version. The value never changes:
-- the trigger below refuses any change, so it needs no audit entry.

ALTER TABLE applications ADD COLUMN requisitions TEXT
    CHECK (requisitions IS NULL OR length(trim(requisitions)) BETWEEN 1 AND 500);

CREATE TRIGGER fixed_requisitions_applications BEFORE UPDATE OF requisitions ON applications
WHEN OLD.requisitions IS NOT NEW.requisitions
BEGIN SELECT RAISE(ABORT, 'requisitions can not be changed'); END;
