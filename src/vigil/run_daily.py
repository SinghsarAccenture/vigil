"""Vigil daily run: scrape every enabled source, save changes, classify, report.

Usage (from the project root):
    python src/vigil/run_daily.py                       normal daily run
    python src/vigil/run_daily.py --baseline            first-time history load, no AI step
    python src/vigil/run_daily.py --classify-since 2026-09-01
    python src/vigil/run_daily.py --max-pages 3         quick partial test run

Steps: sync sources -> for each enabled source: close abandoned runs, start a
logged run, scrape, check the row count, save (all-or-nothing), close the run
-> AI step -> stale-source check -> console report.

Safe to run repeatedly (e.g. from a daily scheduled task): unchanged entries
only get their "last seen" time updated. Exit code 0 means everything worked;
1 means something needs attention (a failed or aborted scrape, a stale source,
or the AI step stopping on repeated errors).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in (None, ""):
    # Allow `python src/vigil/run_daily.py` as well as `python -m vigil.run_daily`.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from vigil.ingestion.base import HttpClient, ScrapeError, Source  # noqa: E402
from vigil.ingestion.sec_rulemaking import SecRulemaking  # noqa: E402
from vigil.llm import classifier as llm  # noqa: E402
from vigil.storage import db  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = PROJECT_ROOT / "vigil.db"

SCRAPERS: dict[str, type[Source]] = {"sec_rulemaking": SecRulemaking}

ROW_DROP_ABORT_RATIO = 0.10
LIST_LIMIT = 25

CHANGE_FIELDS = (
    ("issue_date", "date"),
    ("title", "title"),
    ("division", "division"),
    ("status", "status"),
    ("file_numbers_json", "file numbers"),
    ("reference_numbers_json", "release numbers"),
)


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

def load_env(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into os.environ (existing values win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_sources(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("sources") or [])


def build_scrapers(sources: list[Mapping[str, Any]], max_pages: int | None
                   ) -> tuple[dict[str, Source], dict[str, str]]:
    """Create a scraper for each enabled source that has one.

    Returns (scrapers, problems); problems explains, per source, why no
    scraper could be built, so the report can say so.
    """
    scrapers: dict[str, Source] = {}
    problems: dict[str, str] = {}
    for s in sources:
        if not s.get("enabled"):
            continue
        sid = s["source_id"]
        cls = SCRAPERS.get(sid)
        if cls is None:
            problems[sid] = "enabled in sources.yaml but no scraper is implemented for it"
            continue
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if "@" not in user_agent:
            problems[sid] = ("SEC_USER_AGENT is missing or has no contact e-mail; add e.g. "
                             "SEC_USER_AGENT=Vigil regulatory monitor (you@example.com) to .env")
            continue
        scrapers[sid] = cls(HttpClient(user_agent), s["base_url"], max_pages=max_pages)
    return scrapers, problems


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_row_count(previous: int | None, current: int,
                    abort_ratio: float = ROW_DROP_ABORT_RATIO) -> tuple[str, str | None]:
    """Compare this scrape's entry count with the last successful one.

    Returns ("ok", None), ("warn", message) for any decrease, or
    ("abort", message) when the drop is larger than abort_ratio.
    """
    if previous is None or current >= previous:
        return "ok", None
    drop = previous - current
    ratio = drop / previous
    change = f"{previous:,} -> {current:,} (-{drop:,}, -{ratio:.1%})"
    if ratio > abort_ratio:
        return "abort", f"row count dropped {change}, more than the {abort_ratio:.0%} limit"
    return "warn", f"row count decreased {change}"


# --------------------------------------------------------------------------
# One source
# --------------------------------------------------------------------------

@dataclass
class SourceOutcome:
    source_id: str
    name: str
    status: str  # success | failed | aborted
    lines: list[str] = field(default_factory=list)


def scrape_source(conn: sqlite3.Connection, source: Mapping[str, Any], scraper: Source,
                  mode: str, partial: bool) -> SourceOutcome:
    """Run one logged scrape of one source and describe what happened."""
    sid, name = source["source_id"], source["name"]
    out = SourceOutcome(sid, name, "failed")

    for r in db.close_abandoned_runs(conn, sid):
        out.lines.append(f"NOTE     Closed abandoned run #{r['run_id']} (started {r['started_at']}) as failed.")
    try:
        run_id = db.start_run(conn, sid, mode)
    except db.RunInProgressError as exc:
        out.lines.append(f"FAILED   Not started: {exc}. Another Vigil run may still be working.")
        return out

    started_at = db.get_run(conn, run_id)["started_at"]
    clock = time.monotonic()
    try:
        result = scraper.scrape()
        unique = len(result.records)
        previous = db.last_successful_run(conn, sid, before_run_id=run_id)
        level, message = ("ok", None) if partial else check_row_count(
            previous["rows_seen"] if previous else None, unique)

        if level == "abort":
            explanation = ("This usually means the scraper broke (site redesign, blocked request, "
                           "or pages failed to load), not that the regulator removed entries.")
            db.finish_run(conn, run_id, "failed", rows_seen=unique, rows_duplicate=result.duplicates,
                          rows_fallback_id=result.fallback_ids, error=f"ABORTED: {message}. {explanation}")
            out.status = "aborted"
            out.lines.append(f"ABORTED  Run #{run_id}: {message}.")
            out.lines.append(f"         {explanation}")
            out.lines.append(f"         Run #{run_id} marked FAILED; nothing from it was saved.")
            return out

        counts = {"new": 0, "changed": 0, "unchanged": 0}
        with conn:
            observed_at = db.utc_now()
            for record, raw in result.records:
                counts[db.record_observation(conn, sid, run_id, record, raw, observed_at)] += 1
        db.finish_run(conn, run_id, "success", rows_seen=unique, rows_new=counts["new"],
                      rows_changed=counts["changed"], rows_unchanged=counts["unchanged"],
                      rows_duplicate=result.duplicates, rows_fallback_id=result.fallback_ids)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        db.finish_run(conn, run_id, "failed", error=detail)
        out.lines.append(f"FAILED   Run #{run_id}: {detail}")
        out.lines.append(f"         Run #{run_id} marked FAILED; nothing from it was saved.")
        if not isinstance(exc, ScrapeError):
            out.lines.extend("         " + line for line in traceback.format_exc().rstrip().splitlines())
        return out

    out.status = "success"
    took = time.monotonic() - clock
    out.lines.append(f"OK       Run #{run_id}: {result.pages} page(s) in {took:.0f}s"
                     + (" (PARTIAL: page limit set, row-count and missing-entry checks skipped)" if partial else ""))
    out.lines.append(f"         {unique:,} entries: {counts['new']:,} new, {counts['changed']:,} changed, "
                     f"{counts['unchanged']:,} unchanged | {result.duplicates} repeated rows dropped | "
                     f"{result.fallback_ids} fallback IDs")
    for warning in result.warnings:
        out.lines.append(f"WARNING  {warning}")
    if level == "warn":
        out.lines.append(f"WARNING  {message}. Run kept.")
    if not partial:
        out.lines.extend(_missing_lines(conn, sid, started_at, previous))
    out.lines.extend(_change_lines(conn, run_id, mode))
    return out


def _missing_lines(conn, sid, started_at, previous) -> list[str]:
    missing = db.documents_not_seen(conn, sid, started_at)
    if not missing:
        return []
    newly = [d for d in missing if previous is not None and d["last_seen_at"] >= previous["started_at"]]
    lines = [f"WARNING  {len(missing):,} entries not seen this run ({len(newly):,} newly missing since the last run)."]
    lines.extend(_entry_line(d, f"last seen {d['last_seen_at'][:10]}") for d in newly[:LIST_LIMIT])
    if len(newly) > LIST_LIMIT:
        lines.append(f"           ... and {len(newly) - LIST_LIMIT:,} more")
    return lines


def _change_lines(conn, run_id, mode) -> list[str]:
    versions = db.versions_in_run(conn, run_id)
    new = [v for v in versions if v["change_type"] == "new"]
    changed = [v for v in versions if v["change_type"] == "changed"]
    lines: list[str] = []
    if new and mode == "baseline":
        lines.append(f"         Baseline: {len(new):,} entries loaded as history (not listed, not sent to the AI step).")
    elif new:
        lines.append("NEW")
        lines.extend(_entry_line(v) for v in new[:LIST_LIMIT])
        if len(new) > LIST_LIMIT:
            lines.append(f"           ... and {len(new) - LIST_LIMIT:,} more")
    if changed:
        lines.append("CHANGED")
        for v in changed[:LIST_LIMIT]:
            lines.append(_entry_line(v))
            for column, label in CHANGE_FIELDS:
                if v[column] != v[f"prev_{column}"]:
                    lines.append(f"             {label}: {_show(v['prev_' + column])} -> {_show(v[column])}")
        if len(changed) > LIST_LIMIT:
            lines.append(f"           ... and {len(changed) - LIST_LIMIT:,} more")
    if mode != "baseline":
        flags = db.final_after_proposed(conn, run_id)
        if flags:
            lines.append("FINAL RULE AFTER PROPOSAL")
            for f in flags:
                why = (f"status changed from {f['preceded_by']}" if f["signal"] == "status_changed"
                       else f"rulemaking {f['group_key']} previously had: {f['preceded_by']}")
                lines.append(f"           {f['issue_date']}  {f['status']}  {f['title']}")
                lines.append(f"             {why}")
    return lines


def _entry_line(row, extra: str = "") -> str:
    links = json.loads(row["links_json"] or "[]")
    parts = [row["issue_date"] or "????-??-??", row["status"] or "(no status)", row["title"]]
    if extra:
        parts.append(f"[{extra}]")
    line = "           " + "  ".join(parts)
    return line + (f"\n             {links[0]}" if links else "")


def _show(value: Any) -> str:
    if isinstance(value, str) and value.startswith("["):
        items = json.loads(value)
        return ", ".join(items) if items else "(none)"
    return repr(value) if value else "(none)"


# --------------------------------------------------------------------------
# Whole run
# --------------------------------------------------------------------------

def run(conn: sqlite3.Connection, scrapers: Mapping[str, Source], *, mode: str = "daily",
        classify_since: str | None = None, classifier: llm.Classifier = llm.PLACEHOLDER,
        partial: bool = False, problems: Mapping[str, str] | None = None,
        out: Callable[[str], None] = print) -> int:
    """Scrape, save, classify and report. Returns the process exit code."""
    problems = problems or {}
    exit_code = 0
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out(f"Vigil run | {stamp} | mode: {mode}" + (f" | classify since {classify_since}" if classify_since else ""))
    out("=" * 78)

    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY source_id").fetchall()
    for source in sources:
        out(f"\n{source['name']} ({source['source_id']})")
        scraper = scrapers.get(source["source_id"])
        if scraper is None:
            reason = problems.get(source["source_id"], "no scraper is implemented for it")
            out(f"FAILED   Not run: {reason}.")
            exit_code = 1
            continue
        outcome = scrape_source(conn, source, scraper, mode, partial)
        for line in outcome.lines:
            out(line)
        if outcome.status != "success":
            exit_code = 1

    out("\nAI step")
    if mode == "baseline":
        out("         Skipped (baseline run). To assess recent entries next, run with --classify-since YYYY-MM-DD.")
    else:
        label = ("placeholder mode: answers are 'unknown', not real assessments"
                 if classifier.model == llm.PLACEHOLDER.model else f"model {classifier.model}")
        summary = llm.classify_pending(conn, classifier, classify_since)
        out(f"         ({label}) {summary.pending:,} waiting, {summary.succeeded:,} answered, "
            f"{summary.failed:,} failed")
        if summary.stopped_early:
            out(f"FAILED   AI step stopped after {llm.MAX_CONSECUTIVE_FAILURES} failures in a row; "
                f"the rest stay queued for the next run. Last error: {summary.last_error}")
            exit_code = 1
        elif summary.failed:
            out(f"WARNING  {summary.failed} AI attempt(s) failed and will be retried next run. "
                f"Last error: {summary.last_error}")

    out("\nStale sources (dead man's switch)")
    stale = db.stale_sources(conn)
    if not stale:
        out("         None: every enabled source has a recent successful run.")
    for s in stale:
        when = s["last_success_at"] or "never"
        out(f"STALE    {s['name']}: no successful run within {s['expected_every_hours']}h (last success: {when}).")
        exit_code = 1

    out("\n" + "=" * 78)
    out("RESULT: OK" if exit_code == 0 else
        "RESULT: NEEDS ATTENTION (exit code 1). See FAILED / ABORTED / STALE lines above.")
    return exit_code


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    def iso_date(text: str) -> str:
        try:
            return dt.date.fromisoformat(text).isoformat()
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")

    parser = argparse.ArgumentParser(description="Vigil daily regulatory scrape and report.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true",
                       help="first-time history load: save everything, skip the AI step")
    group.add_argument("--classify-since", type=iso_date, metavar="YYYY-MM-DD",
                       help="send entries issued on or after this date to the AI step (history included)")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help="only read the first N pages of each source (testing; skips row-count checks)")
    parser.add_argument("--db", type=Path, help="database file (default: VIGIL_DB_PATH or vigil.db)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args(argv)
    load_env(ENV_PATH)
    db_path = args.db or Path(os.environ.get("VIGIL_DB_PATH") or DEFAULT_DB_PATH)

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        sources = load_sources(SOURCES_PATH)
        db.sync_sources(conn, sources)
        scrapers, problems = build_scrapers(sources, args.max_pages)
        return run(conn, scrapers, mode="baseline" if args.baseline else "daily",
                   classify_since=args.classify_since, partial=args.max_pages is not None,
                   problems=problems)
    except (db.SchemaVersionError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"FAILED   Vigil could not start: {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
