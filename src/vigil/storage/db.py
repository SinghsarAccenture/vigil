"""SQLite storage layer for Vigil.

Plain sqlite3, no ORM. All SQL DDL lives in schema.sql.

Transaction convention: functions that record run bookkeeping or
classifications (sync_sources, start_run, finish_run, record_classification)
commit immediately, so that they survive a crash. record_observation does NOT
commit; callers wrap a whole scrape in ``with conn:`` so that a run is stored
all-or-nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Must equal the version inserted at the top of schema.sql.
SCHEMA_VERSION = 1

# Upgrade steps for existing databases: {target_version: SQL that upgrades
# from target_version - 1}. Every design change adds one entry here.
MIGRATIONS: dict[int, str] = {}

HASHED_FIELDS = (
    "issue_date",
    "title",
    "division",
    "status",
    "links",
    "file_numbers",
    "reference_numbers",
)

ABANDONED_RUN_HOURS = 6

SEC_FINAL_STATUSES = ("Final Rule", "Interim Final Rule")
SEC_PROPOSED_STATUSES = ("Proposed Rule",)


def utc_now() -> str:
    """Return the current UTC time as a fixed-width ISO-8601 string."""
    return to_timestamp(dt.datetime.now(dt.timezone.utc))


def to_timestamp(moment: dt.datetime) -> str:
    """Format an aware datetime as the fixed-width UTC string used in the DB.

    Fixed width matters: timestamps are compared as strings in SQL.
    """
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and dict-like rows."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class SchemaVersionError(RuntimeError):
    """The database's design version does not match what this code expects."""


def schema_version(conn: sqlite3.Connection) -> int | None:
    """Version stamped on the database, or None if it has no stamp."""
    has_stamp = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_info'"
    ).fetchone()
    return conn.execute("SELECT version FROM schema_info").fetchone()[0] if has_stamp else None


def init_db(conn: sqlite3.Connection) -> None:
    """Prepare the database for use, never discarding stored data.

    - Empty database: build it from schema.sql (the latest design).
    - Older version: apply each missing upgrade step from MIGRATIONS in order,
      each in its own transaction.
    - Current version: nothing to do.
    - Newer version, missing stamp, or missing upgrade step: stop with
      SchemaVersionError rather than guess.
    """
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone()
    if not has_tables:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return

    current = schema_version(conn)
    if current is None:
        raise SchemaVersionError(
            "database has tables but no version stamp (it predates version tracking); "
            "rebuild it or stamp it manually"
        )
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database is at design version {current} but this code only knows up to {SCHEMA_VERSION}; "
            "update the code before using this database"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        if version not in MIGRATIONS:
            raise SchemaVersionError(f"no upgrade step from design version {version - 1} to {version}")
        try:
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version]};\nUPDATE schema_info SET version = {version};\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            conn.rollback()
            raise SchemaVersionError(f"upgrade to design version {version} failed; nothing changed: {exc}") from exc


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def sync_sources(conn: sqlite3.Connection, sources: Iterable[Mapping[str, Any]]) -> None:
    """Upsert the source registry; sources missing from the registry are disabled."""
    sources = list(sources)
    if not sources:
        raise ValueError("source registry is empty; refusing to disable every source")
    with conn:
        for s in sources:
            conn.execute(
                """
                INSERT INTO sources (source_id, name, regulator, jurisdiction, scraper_type,
                                     base_url, expected_every_hours, enabled)
                VALUES (:source_id, :name, :regulator, :jurisdiction, :scraper_type,
                        :base_url, :expected_every_hours, :enabled)
                ON CONFLICT (source_id) DO UPDATE SET
                    name = excluded.name,
                    regulator = excluded.regulator,
                    jurisdiction = excluded.jurisdiction,
                    scraper_type = excluded.scraper_type,
                    base_url = excluded.base_url,
                    expected_every_hours = excluded.expected_every_hours,
                    enabled = excluded.enabled
                """,
                {**s, "enabled": 1 if s["enabled"] else 0},
            )
        ids = [s["source_id"] for s in sources]
        conn.execute(
            f"UPDATE sources SET enabled = 0 WHERE source_id NOT IN ({_placeholders(ids)})",
            ids,
        )


# --------------------------------------------------------------------------
# Scrape runs
# --------------------------------------------------------------------------

class RunInProgressError(RuntimeError):
    """Another scrape of the same source is still running."""


def close_abandoned_runs(conn: sqlite3.Connection, source_id: str,
                         now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Mark runs left 'running' for longer than ABANDONED_RUN_HOURS as failed.

    Such runs belong to a process that was killed or crashed. Returns the
    runs that were closed so the caller can report them.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = to_timestamp(now - dt.timedelta(hours=ABANDONED_RUN_HOURS))
    with conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT run_id, started_at FROM scrape_runs WHERE source_id = ? AND status = 'running' AND started_at < ?",
            (source_id, cutoff),
        )]
        for r in rows:
            conn.execute(
                "UPDATE scrape_runs SET status = 'failed', finished_at = ?, error = ? WHERE run_id = ?",
                (to_timestamp(now),
                 f"abandoned: still 'running' after {ABANDONED_RUN_HOURS}h; the process stopped before finishing",
                 r["run_id"]),
            )
    return rows


def start_run(conn: sqlite3.Connection, source_id: str, mode: str = "daily",
              now: str | None = None) -> int:
    """Log the start of a scrape and return its run_id.

    Raises RunInProgressError if the source already has a run marked
    'running' (call close_abandoned_runs first to clear crashed ones).
    The check and insert happen under one write lock, so two processes
    cannot both start.
    """
    if conn.in_transaction:
        raise RuntimeError("start_run must not be called inside an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT run_id, started_at FROM scrape_runs WHERE source_id = ? AND status = 'running'",
            (source_id,),
        ).fetchone()
        if active:
            raise RunInProgressError(
                f"{source_id}: run {active['run_id']} (started {active['started_at']}) is still running"
            )
        cur = conn.execute(
            "INSERT INTO scrape_runs (source_id, mode, started_at, status) VALUES (?, ?, ?, 'running')",
            (source_id, mode, now or utc_now()),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, *,
               rows_seen: int | None = None, rows_new: int | None = None,
               rows_changed: int | None = None, rows_unchanged: int | None = None,
               rows_duplicate: int | None = None, rows_fallback_id: int | None = None,
               error: str | None = None, now: str | None = None) -> None:
    """Close a running scrape as 'success' or 'failed'. A run can be closed only once."""
    if status not in ("success", "failed"):
        raise ValueError(f"invalid final status {status!r}")
    with conn:
        cur = conn.execute(
            """
            UPDATE scrape_runs
            SET finished_at = ?, status = ?, rows_seen = ?, rows_new = ?, rows_changed = ?,
                rows_unchanged = ?, rows_duplicate = ?, rows_fallback_id = ?, error = ?
            WHERE run_id = ? AND status = 'running'
            """,
            (now or utc_now(), status, rows_seen, rows_new, rows_changed, rows_unchanged,
             rows_duplicate, rows_fallback_id, error, run_id),
        )
    if cur.rowcount != 1:
        raise RuntimeError(f"scrape run {run_id} is not running; cannot close it")


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM scrape_runs WHERE run_id = ?", (run_id,)).fetchone()


def last_successful_run(conn: sqlite3.Connection, source_id: str,
                        before_run_id: int | None = None) -> sqlite3.Row | None:
    """Most recent successful run for a source, optionally earlier than a given run."""
    return conn.execute(
        """
        SELECT * FROM scrape_runs
        WHERE source_id = ? AND status = 'success' AND (? IS NULL OR run_id < ?)
        ORDER BY run_id DESC LIMIT 1
        """,
        (source_id, before_run_id, before_run_id),
    ).fetchone()


# --------------------------------------------------------------------------
# Documents and versions
# --------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _clean_list(values: Iterable[Any] | None) -> list[str]:
    return sorted({_clean_text(v) for v in (values or []) if _clean_text(v)})


def _clean_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if dt.date.fromisoformat(text).isoformat() != text:
        raise ValueError(f"issue_date must be YYYY-MM-DD, got {value!r}")
    return text


def canonicalise(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the hashed fields in canonical form.

    Collapses whitespace, sorts and de-duplicates lists, and requires ISO dates,
    so cosmetic noise never registers as a change. If the record carries
    `link_keys` (stable forms of its links), those are hashed instead of the
    real `links`, so a regulator moving a page does not count as a change.
    """
    link_keys = record.get("link_keys")
    return {
        "issue_date": _clean_date(record.get("issue_date")),
        "title": _clean_text(record.get("title")),
        "division": _clean_text(record.get("division")),
        "status": _clean_text(record.get("status")),
        "links": _clean_list(record.get("links") if link_keys is None else link_keys),
        "file_numbers": _clean_list(record.get("file_numbers")),
        "reference_numbers": _clean_list(record.get("reference_numbers")),
    }


def content_hash(record: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical form of the hashed fields."""
    payload = json.dumps(canonicalise(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_observation(conn: sqlite3.Connection, source_id: str, run_id: int,
                       record: Mapping[str, Any], raw: Any, observed_at: str) -> str:
    """Store one normalised record seen during a run. Does not commit.

    Returns 'new' (document created with version 1), 'changed' (new version
    linked to the previous one) or 'unchanged' (only last_seen_at bumped).
    """
    external_id = record["external_id"]
    group_key = record.get("group_key")
    canon = canonicalise(record)
    digest = content_hash(record)

    doc = conn.execute(
        "SELECT document_id, group_key, last_seen_at FROM documents WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()

    if doc is None:
        cur = conn.execute(
            """
            INSERT INTO documents (source_id, external_id, group_key, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, external_id, group_key, observed_at, observed_at),
        )
        _insert_version(conn, cur.lastrowid, 1, run_id, "new", observed_at, canon, record, raw, digest, None)
        return "new"

    if doc["group_key"] != group_key:
        raise ValueError(
            f"{source_id}/{external_id}: group_key changed from {doc['group_key']!r} to {group_key!r}"
        )

    latest = conn.execute(
        """
        SELECT version_id, version_no, content_hash FROM document_versions
        WHERE document_id = ? ORDER BY version_no DESC LIMIT 1
        """,
        (doc["document_id"],),
    ).fetchone()

    if observed_at > doc["last_seen_at"]:
        conn.execute("UPDATE documents SET last_seen_at = ? WHERE document_id = ?",
                     (observed_at, doc["document_id"]))

    if latest["content_hash"] == digest:
        return "unchanged"

    _insert_version(conn, doc["document_id"], latest["version_no"] + 1, run_id, "changed",
                    observed_at, canon, record, raw, digest, latest["version_id"])
    return "changed"


def _insert_version(conn, document_id, version_no, run_id, change_type, observed_at,
                    canon, record, raw, digest, previous_version_id) -> None:
    conn.execute(
        """
        INSERT INTO document_versions (
            document_id, version_no, run_id, change_type, observed_at, issue_date, title, division,
            status, file_numbers_json, file_numbers_raw, reference_numbers_json, links_json, raw_json,
            content_hash, previous_version_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, version_no, run_id, change_type, observed_at, canon["issue_date"], canon["title"],
         canon["division"], canon["status"], json.dumps(canon["file_numbers"]),
         str(record.get("file_numbers_raw") or ""), json.dumps(canon["reference_numbers"]),
         json.dumps(_clean_list(record.get("links"))), json.dumps(raw, sort_keys=True, ensure_ascii=False),
         digest, previous_version_id),
    )


def get_document(conn: sqlite3.Connection, source_id: str, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE source_id = ? AND external_id = ?", (source_id, external_id)
    ).fetchone()


def get_versions(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_no", (document_id,)
    ).fetchall()


def versions_in_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Versions created by a run, each with its previous version's fields as prev_*."""
    return conn.execute(
        """
        SELECT v.*, d.external_id, d.group_key,
               p.issue_date AS prev_issue_date, p.title AS prev_title, p.division AS prev_division,
               p.status AS prev_status, p.file_numbers_json AS prev_file_numbers_json,
               p.reference_numbers_json AS prev_reference_numbers_json, p.links_json AS prev_links_json
        FROM document_versions v
        JOIN documents d ON d.document_id = v.document_id
        LEFT JOIN document_versions p ON p.version_id = v.previous_version_id
        WHERE v.run_id = ?
        ORDER BY v.issue_date DESC, v.version_id
        """,
        (run_id,),
    ).fetchall()


def current_link(conn: sqlite3.Connection, document_id: int) -> str | None:
    """Best working link for a document.

    Regulators such as the SEC move a group's page when they add to it, so an
    old document's stored link can go dead. Documents in a group share one
    page; this returns the page address most recently observed anywhere in the
    group, keeping the document's own '#fragment' so it still points at the
    right entry. Without a group, it is the document's own latest link.
    """
    doc = conn.execute("SELECT source_id, group_key FROM documents WHERE document_id = ?",
                       (document_id,)).fetchone()
    own = conn.execute(
        "SELECT links_json FROM document_versions WHERE document_id = ? ORDER BY version_no DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    own_links = json.loads(own["links_json"]) if own else []
    if not own_links:
        return None
    if doc["group_key"] is None:
        return own_links[0]

    newest = conn.execute(
        """
        SELECT v.links_json FROM document_versions v
        JOIN documents d ON d.document_id = v.document_id
        WHERE d.source_id = ? AND d.group_key = ? AND v.links_json <> '[]'
        ORDER BY v.observed_at DESC, v.version_id DESC LIMIT 1
        """,
        (doc["source_id"], doc["group_key"]),
    ).fetchone()
    page = json.loads(newest["links_json"])[0].split("#", 1)[0]
    _, hash_sign, fragment = own_links[0].partition("#")
    return page + hash_sign + fragment


# --------------------------------------------------------------------------
# Classifications
# --------------------------------------------------------------------------

def pending_classifications(conn: sqlite3.Connection, prompt_version: str,
                            classify_since: str | None = None) -> list[sqlite3.Row]:
    """Current versions lacking a successful classification for prompt_version.

    Superseded versions are skipped. Without classify_since, versions first
    loaded by a baseline run are excluded; with it, only versions whose
    issue_date is on or after that date are returned (baseline or not).
    Failed attempts do not count, so failures are retried on the next run.
    """
    since = _clean_date(classify_since)
    return conn.execute(
        """
        SELECT v.*, d.source_id, d.external_id, d.group_key
        FROM document_versions v
        JOIN documents d ON d.document_id = v.document_id
        JOIN scrape_runs r ON r.run_id = v.run_id
        WHERE v.version_no = (SELECT MAX(v2.version_no) FROM document_versions v2
                              WHERE v2.document_id = v.document_id)
          AND NOT EXISTS (SELECT 1 FROM classifications c
                          WHERE c.version_id = v.version_id AND c.prompt_version = ?
                            AND c.error IS NULL)
          AND ((? IS NULL AND r.mode <> 'baseline') OR (? IS NOT NULL AND v.issue_date >= ?))
        ORDER BY v.issue_date DESC, v.version_id
        """,
        (prompt_version, since, since, since),
    ).fetchall()


def record_classification(conn: sqlite3.Connection, version_id: int, model: str, prompt_version: str,
                          result: Mapping[str, Any] | None = None, error: str | None = None,
                          now: str | None = None) -> int:
    """Append one classification attempt: a validated result, or an error.

    result must carry relevant, confidence and reason; document_type and
    requires_human_review are optional. The full result is stored in output_json.
    """
    if (result is None) == (error is None):
        raise ValueError("pass exactly one of result or error")
    r = result or {}
    with conn:
        cur = conn.execute(
            """
            INSERT INTO classifications (version_id, classified_at, model, prompt_version, relevant,
                                         confidence, reason, document_type, requires_human_review,
                                         output_json, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (version_id, now or utc_now(), model, prompt_version,
             _as_flag(r.get("relevant")) if result else None,
             r.get("confidence"), r.get("reason"), r.get("document_type"),
             _as_flag(r.get("requires_human_review")),
             json.dumps(result, sort_keys=True, ensure_ascii=False) if result else None,
             error),
        )
    return cur.lastrowid


def _as_flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------

def stale_sources(conn: sqlite3.Connection, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Dead man's switch: enabled sources with no successful run within expected_every_hours."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = conn.execute(
        """
        SELECT s.source_id, s.name, s.expected_every_hours, MAX(r.finished_at) AS last_success_at
        FROM sources s
        LEFT JOIN scrape_runs r ON r.source_id = s.source_id AND r.status = 'success'
        WHERE s.enabled = 1
        GROUP BY s.source_id, s.name, s.expected_every_hours
        ORDER BY s.source_id
        """
    ).fetchall()
    stale = []
    for row in rows:
        cutoff = to_timestamp(now - dt.timedelta(hours=row["expected_every_hours"]))
        if row["last_success_at"] is None or row["last_success_at"] < cutoff:
            stale.append(dict(row))
    return stale


def documents_not_seen(conn: sqlite3.Connection, source_id: str, since: str) -> list[sqlite3.Row]:
    """Current documents of a source whose last_seen_at is older than `since` (e.g. a run start)."""
    return conn.execute(
        """
        SELECT * FROM v_current
        WHERE source_id = ? AND last_seen_at < ?
        ORDER BY issue_date DESC, external_id
        """,
        (source_id, since),
    ).fetchall()


def final_after_proposed(conn: sqlite3.Connection, run_id: int,
                         final_statuses: Sequence[str] = SEC_FINAL_STATUSES,
                         proposed_statuses: Sequence[str] = SEC_PROPOSED_STATUSES) -> list[dict[str, Any]]:
    """Final-type versions created in a run that follow a proposal.

    Two cases, reported via `signal`:
      - 'final_in_group': a new document with a final status whose group
        already contains a proposed-status document dated on or before it;
      - 'status_changed': an existing document whose status moved from a
        proposed status to a final status in this run.
    """
    finals, proposed = list(final_statuses), list(proposed_statuses)
    rows = conn.execute(
        f"""
        SELECT 'final_in_group' AS signal, d.source_id, d.group_key, d.external_id, v.version_id,
               v.issue_date, v.title, v.status,
               (SELECT GROUP_CONCAT(p.external_id, ', ') FROM v_current p
                WHERE p.source_id = d.source_id AND p.group_key = d.group_key
                  AND p.document_id <> d.document_id AND p.status IN ({_placeholders(proposed)})
                  AND (p.issue_date IS NULL OR v.issue_date IS NULL OR p.issue_date <= v.issue_date)
               ) AS preceded_by
        FROM document_versions v
        JOIN documents d ON d.document_id = v.document_id
        WHERE v.run_id = ? AND v.change_type = 'new' AND d.group_key IS NOT NULL
          AND v.status IN ({_placeholders(finals)})
        UNION ALL
        SELECT 'status_changed', d.source_id, d.group_key, d.external_id, v.version_id,
               v.issue_date, v.title, v.status, prev.status
        FROM document_versions v
        JOIN documents d ON d.document_id = v.document_id
        JOIN document_versions prev ON prev.version_id = v.previous_version_id
        WHERE v.run_id = ? AND v.change_type = 'changed'
          AND v.status IN ({_placeholders(finals)}) AND prev.status IN ({_placeholders(proposed)})
        """,
        [*proposed, run_id, *finals, run_id, *finals, *proposed],
    ).fetchall()
    return [dict(r) for r in rows if r["preceded_by"]]


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values) or "NULL"
