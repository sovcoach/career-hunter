-- 0024_jd_unavailable (search track): the current JD-unavailable reason (ADR-024,
-- amended).
--
-- applications.jd_unavailable_reason mirrors the latest row of the EI record's
-- jd_unavailable table (EI migration 0003): why the application's JD can't be kept,
-- such as a posting removed before it was captured. Null when it was never marked. An
-- application that is only in search.db keeps its reason here alone. A JD attached
-- later takes its place wherever the JD is shown; the reason stays. This migration only
-- adds a column. EI files are not changed by it.

ALTER TABLE applications
    ADD COLUMN jd_unavailable_reason TEXT CHECK (jd_unavailable_reason IS NULL OR (
        length(jd_unavailable_reason) BETWEEN 1 AND 200
        AND jd_unavailable_reason = trim(jd_unavailable_reason)));
