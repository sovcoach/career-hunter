-- 0035_board_status (search track): what the watcher last found about a board, by
-- company.
--
-- One row per company whose Workday board has `ats_facets` in its watchlist entry. It
-- holds only the facet result of the board's last read: `facet_status` is `matched`
-- when every facet and value name was found in the board's facets (the list was read
-- filtered), or `unmatched` when one or more were not (the list was read unfiltered).
-- `unmatched` is a JSON array of those names, each `<facet>: <value>`, empty when
-- matched. `checked_at` is when the list was read.
--
-- The watcher sets the row each run it reads the board, and removes it when the
-- board's entry no longer has `ats_facets`. A board that could not be read keeps its
-- last row. `doctor` warns for each unmatched name. No facet id, posting or text is
-- stored.
-- EI files are not changed by this migration.

CREATE TABLE board_status (
    company_id   INTEGER PRIMARY KEY REFERENCES companies (id),
    facet_status TEXT    NOT NULL CHECK (facet_status IN ('matched', 'unmatched')),
    unmatched    TEXT    NOT NULL DEFAULT '[]'
                         CHECK (json_valid(unmatched) AND json_type(unmatched) = 'array'),
    checked_at   TEXT    NOT NULL CHECK (checked_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((facet_status = 'matched') = (json_array_length(unmatched) = 0))
);
