-- Vigil schema (SQLite). Kept to portable SQL where possible; the append-only
-- triggers and INTEGER PRIMARY KEY rowids are the SQLite-specific parts to
-- rewrite for Postgres (plpgsql triggers, GENERATED ... AS IDENTITY).
-- Timestamps are UTC ISO-8601 strings; dates are YYYY-MM-DD.
--
-- This file always describes the LATEST design and is only used to build a
-- brand-new database. Existing databases are upgraded by the steps in
-- db.MIGRATIONS. When changing this file: bump the version below AND
-- db.SCHEMA_VERSION, and add the matching upgrade step.

CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);
INSERT INTO schema_info (version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_info);

CREATE TABLE IF NOT EXISTS sources (
    source_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    regulator            TEXT NOT NULL,
    jurisdiction         TEXT NOT NULL,
    scraper_type         TEXT NOT NULL CHECK (scraper_type IN ('html', 'api', 'rss', 'pdf_index')),
    base_url             TEXT NOT NULL,
    expected_every_hours INTEGER NOT NULL CHECK (expected_every_hours > 0),
    enabled              INTEGER NOT NULL CHECK (enabled IN (0, 1))
);

-- rows_seen counts unique records after de-duplication (= new + changed + unchanged).
CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id           INTEGER PRIMARY KEY,
    source_id        TEXT NOT NULL REFERENCES sources (source_id),
    mode             TEXT NOT NULL CHECK (mode IN ('daily', 'baseline')),
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    rows_seen        INTEGER,
    rows_new         INTEGER,
    rows_changed     INTEGER,
    rows_unchanged   INTEGER,
    rows_duplicate   INTEGER,
    rows_fallback_id INTEGER,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS ix_scrape_runs_source ON scrape_runs (source_id, status, finished_at);

-- group_key links related documents (for the SEC: all releases on one rulemaking).
CREATE TABLE IF NOT EXISTS documents (
    document_id   INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources (source_id),
    external_id   TEXT NOT NULL,
    group_key     TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS ix_documents_group ON documents (source_id, group_key);

CREATE TABLE IF NOT EXISTS document_versions (
    version_id             INTEGER PRIMARY KEY,
    document_id            INTEGER NOT NULL REFERENCES documents (document_id),
    version_no             INTEGER NOT NULL CHECK (version_no >= 1),
    run_id                 INTEGER NOT NULL REFERENCES scrape_runs (run_id),
    change_type            TEXT NOT NULL CHECK (change_type IN ('new', 'changed')),
    observed_at            TEXT NOT NULL,
    issue_date             TEXT,
    title                  TEXT NOT NULL,
    division               TEXT NOT NULL DEFAULT '',
    status                 TEXT NOT NULL DEFAULT '',
    file_numbers_json      TEXT NOT NULL DEFAULT '[]',
    file_numbers_raw       TEXT NOT NULL DEFAULT '',
    reference_numbers_json TEXT NOT NULL DEFAULT '[]',
    links_json             TEXT NOT NULL DEFAULT '[]',
    raw_json               TEXT NOT NULL,
    content_hash           TEXT NOT NULL,
    previous_version_id    INTEGER REFERENCES document_versions (version_id),
    UNIQUE (document_id, version_no),
    CHECK ((version_no = 1) = (previous_version_id IS NULL)),
    CHECK ((version_no = 1) = (change_type = 'new'))
);

CREATE INDEX IF NOT EXISTS ix_versions_run ON document_versions (run_id);

-- A failed attempt has error set and no result; a success has no error.
-- relevant may be empty ("unknown") only for placeholder answers, never for a real model.
CREATE TABLE IF NOT EXISTS classifications (
    classification_id     INTEGER PRIMARY KEY,
    version_id            INTEGER NOT NULL REFERENCES document_versions (version_id),
    classified_at         TEXT NOT NULL,
    model                 TEXT NOT NULL,
    prompt_version        TEXT NOT NULL,
    relevant              INTEGER CHECK (relevant IN (0, 1)),
    confidence            TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    reason                TEXT,
    document_type         TEXT,
    requires_human_review INTEGER CHECK (requires_human_review IN (0, 1)),
    output_json           TEXT,
    error                 TEXT,
    CHECK (error IS NULL OR relevant IS NULL),
    CHECK (error IS NOT NULL OR relevant IS NOT NULL OR model = 'placeholder')
);

-- At most one successful classification per version and prompt version.
CREATE UNIQUE INDEX IF NOT EXISTS ux_classifications_success
    ON classifications (version_id, prompt_version) WHERE error IS NULL;

-- Stub: the alerts workflow is out of scope for now.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id          INTEGER PRIMARY KEY,
    classification_id INTEGER NOT NULL REFERENCES classifications (classification_id),
    priority          TEXT,
    created_at        TEXT NOT NULL,
    status            TEXT,
    summary           TEXT
);

-- Append-only enforcement. documents.last_seen_at is the only mutable column.
CREATE TRIGGER IF NOT EXISTS trg_documents_identity_immutable
BEFORE UPDATE OF document_id, source_id, external_id, group_key, first_seen_at ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents: only last_seen_at may be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_documents_last_seen_monotonic
BEFORE UPDATE OF last_seen_at ON documents
WHEN NEW.last_seen_at < OLD.last_seen_at
BEGIN
    SELECT RAISE(ABORT, 'documents: last_seen_at may not move backwards');
END;

CREATE TRIGGER IF NOT EXISTS trg_documents_no_delete
BEFORE DELETE ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_versions_no_update
BEFORE UPDATE ON document_versions
BEGIN
    SELECT RAISE(ABORT, 'document_versions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_versions_no_delete
BEFORE DELETE ON document_versions
BEGIN
    SELECT RAISE(ABORT, 'document_versions is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_classifications_no_update
BEFORE UPDATE ON classifications
BEGIN
    SELECT RAISE(ABORT, 'classifications is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_classifications_no_delete
BEFORE DELETE ON classifications
BEGIN
    SELECT RAISE(ABORT, 'classifications is append-only');
END;

-- Latest version of every document, joined to its latest successful classification.
CREATE VIEW IF NOT EXISTS v_current AS
SELECT
    d.document_id,
    d.source_id,
    d.external_id,
    d.group_key,
    d.first_seen_at,
    d.last_seen_at,
    v.version_id,
    v.version_no,
    v.change_type,
    v.observed_at,
    v.issue_date,
    v.title,
    v.division,
    v.status,
    v.file_numbers_json,
    v.reference_numbers_json,
    v.links_json,
    c.classification_id,
    c.classified_at,
    c.model,
    c.prompt_version,
    c.relevant,
    c.confidence,
    c.reason,
    c.document_type,
    c.requires_human_review
FROM documents d
JOIN document_versions v
    ON v.document_id = d.document_id
   AND v.version_no = (SELECT MAX(v2.version_no) FROM document_versions v2 WHERE v2.document_id = d.document_id)
LEFT JOIN classifications c
    ON c.classification_id = (
        SELECT c2.classification_id
        FROM classifications c2
        WHERE c2.version_id = v.version_id AND c2.error IS NULL
        ORDER BY c2.classified_at DESC, c2.classification_id DESC
        LIMIT 1
    );

-- Every current document that belongs to a group, for lifecycle views
-- (e.g. a rulemaking's proposal followed by its final rule).
CREATE VIEW IF NOT EXISTS v_group_history AS
SELECT source_id, group_key, issue_date, status, title, external_id, document_id, version_id, first_seen_at
FROM v_current
WHERE group_key IS NOT NULL;
