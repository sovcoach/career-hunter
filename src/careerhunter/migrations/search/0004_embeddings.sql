-- 0004_embeddings (search track): local vectors beside the LLM tier.
--
-- Float32 BLOBs in ordinary tables. No virtual table and no native extension.
-- The resume text and the posting text are not stored. The cosine is a number
-- from -1 to 1, or null when this posting has no comparable pair. EI files are
-- not changed by this migration.

CREATE TABLE resume_embedding (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    content_sha256 TEXT    NOT NULL CHECK (
                       length(content_sha256) = 64
                       AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    dim            INTEGER NOT NULL CHECK (dim > 0),
    vector         BLOB    NOT NULL CHECK (length(vector) = dim * 4),
    embedded_at    TEXT    NOT NULL CHECK (embedded_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE TABLE posting_embeddings (
    posting_id  INTEGER PRIMARY KEY REFERENCES postings (id),
    dim         INTEGER NOT NULL CHECK (dim > 0),
    vector      BLOB    NOT NULL CHECK (length(vector) = dim * 4),
    embedded_at TEXT    NOT NULL CHECK (embedded_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

ALTER TABLE postings ADD COLUMN embedding_score REAL
    CHECK (embedding_score IS NULL OR (embedding_score >= -1 AND embedding_score <= 1));
