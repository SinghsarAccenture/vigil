import os

import pytest

from conftest import SOURCE, make_record
from vigil import run_daily
from vigil.ingestion.base import ScrapeError, ScrapeResult
from vigil.storage import db


class FakeScraper:
    """Stands in for a real scraper; returns whatever records the test sets."""

    def __init__(self, records=(), error=None, duplicates=0, warnings=()):
        self.records, self.error, self.duplicates, self.warnings = list(records), error, duplicates, list(warnings)

    def scrape(self):
        if self.error:
            raise self.error
        return ScrapeResult("test_src", 1, len(self.records) + self.duplicates,
                            [(r, {"row": r["external_id"]}) for r in self.records],
                            self.duplicates, sum(r["external_id"].startswith("fallback:") for r in self.records),
                            self.warnings)


def entries(n, **overrides):
    return [make_record(external_id=f"doc{i}", group_key=f"g{i}", title=f"Rule {i}", **overrides) for i in range(n)]


def go(conn, scraper, **kw):
    lines = []
    code = run_daily.run(conn, {"test_src": scraper}, out=lines.append, **kw)
    return code, "\n".join(lines)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- row-count rule ---------------------------------------------------------------

@pytest.mark.parametrize("previous, current, level", [
    (None, 50, "ok"), (100, 100, "ok"), (100, 120, "ok"),
    (100, 99, "warn"), (100, 90, "warn"), (100, 89, "abort"), (1611, 50, "abort"),
])
def test_check_row_count(previous, current, level):
    assert run_daily.check_row_count(previous, current)[0] == level


def test_check_row_count_messages():
    assert run_daily.check_row_count(1611, 1609)[1] == "row count decreased 1,611 -> 1,609 (-2, -0.1%)"
    assert run_daily.check_row_count(1611, 50)[1] == \
        "row count dropped 1,611 -> 50 (-1,561, -96.9%), more than the 10% limit"


# --- normal runs ------------------------------------------------------------------

def test_first_daily_run_saves_lists_and_classifies(conn):
    code, text = go(conn, FakeScraper(entries(3)))

    assert code == 0 and "RESULT: OK" in text
    assert "3 entries: 3 new, 0 changed, 0 unchanged" in text
    assert "NEW" in text and "Rule 0" in text and "https://example.test/a" in text
    assert "3 waiting, 3 answered, 0 failed" in text and "placeholder mode" in text
    assert count(conn, "classifications") == 3
    run = conn.execute("SELECT * FROM scrape_runs").fetchone()
    assert (run["status"], run["mode"], run["rows_new"]) == ("success", "daily", 3)


def test_rerun_is_idempotent(conn):
    go(conn, FakeScraper(entries(3)))
    code, text = go(conn, FakeScraper(entries(3)))

    assert code == 0
    assert "0 new, 0 changed, 3 unchanged" in text
    assert "0 waiting" in text
    assert (count(conn, "document_versions"), count(conn, "classifications")) == (3, 3)


def test_changed_entry_shows_what_changed(conn):
    go(conn, FakeScraper(entries(2)))
    updated = entries(2)
    updated[1]["title"] = "Rule 1 (Corrected)"
    code, text = go(conn, FakeScraper(updated))

    assert code == 0 and "CHANGED" in text
    assert "title: 'Rule 1' -> 'Rule 1 (Corrected)'" in text
    assert "1 waiting, 1 answered" in text


def test_final_rule_after_proposal_is_flagged(conn):
    go(conn, FakeScraper([make_record()]))
    final = make_record(external_id="s7-01-26#33-2000final", status="Final Rule", issue_date="2026-09-01")
    _, text = go(conn, FakeScraper([make_record(), final]))

    assert "FINAL RULE AFTER PROPOSAL" in text
    assert "rulemaking s7-01-26 previously had: s7-01-26#33-1000proposed" in text


def test_duplicates_fallbacks_and_warnings_are_reported(conn):
    records = entries(2) + [make_record(external_id="fallback:abc", group_key=None)]
    _, text = go(conn, FakeScraper(records, duplicates=4, warnings=["doc1: appears more than once"]))
    assert "4 repeated rows dropped | 1 fallback IDs" in text
    assert "WARNING  doc1: appears more than once" in text


# --- baseline and classify-since ---------------------------------------------------

def test_baseline_loads_history_without_ai(conn):
    code, text = go(conn, FakeScraper(entries(30)), mode="baseline")

    assert code == 0
    assert "30 entries loaded as history (not listed" in text
    assert "Rule 0" not in text
    assert "Skipped (baseline run)" in text
    assert count(conn, "classifications") == 0
    assert conn.execute("SELECT mode FROM scrape_runs").fetchone()[0] == "baseline"


def test_classify_since_after_baseline_only_assesses_recent(conn):
    old = [make_record(external_id="old", issue_date="2020-05-01")]
    recent = [make_record(external_id="recent", issue_date="2026-09-15")]
    go(conn, FakeScraper(old + recent), mode="baseline")
    _, text = go(conn, FakeScraper(old + recent), classify_since="2026-09-01")

    assert "1 waiting, 1 answered" in text
    assert conn.execute("SELECT d.external_id FROM classifications c JOIN document_versions v USING(version_id) "
                        "JOIN documents d USING(document_id)").fetchone()[0] == "recent"


# --- row-count protection -----------------------------------------------------------

def test_small_drop_warns_keeps_run_and_lists_missing(conn):
    go(conn, FakeScraper(entries(20)))
    code, text = go(conn, FakeScraper(entries(19)))

    assert code == 0
    assert "WARNING  row count decreased 20 -> 19 (-1, -5.0%). Run kept." in text
    assert "1 entries not seen this run (1 newly missing since the last run)" in text
    assert "Rule 19" in text
    assert conn.execute("SELECT status FROM scrape_runs ORDER BY run_id DESC").fetchone()[0] == "success"


def test_long_missing_entries_are_counted_but_not_relisted(conn):
    go(conn, FakeScraper(entries(20)))
    go(conn, FakeScraper(entries(19)))
    _, text = go(conn, FakeScraper(entries(19)))
    assert "1 entries not seen this run (0 newly missing since the last run)" in text
    assert "Rule 19" not in text


def test_sharp_drop_aborts_and_saves_nothing(conn):
    go(conn, FakeScraper(entries(20)))
    before = dict(conn.execute("SELECT last_seen_at FROM documents WHERE external_id = 'doc0'").fetchone())
    shrunk = entries(2)
    shrunk[0]["title"] = "Should not be saved"
    code, text = go(conn, FakeScraper(shrunk))

    assert code == 1
    assert "ABORTED" in text and "row count dropped 20 -> 2 (-18, -90.0%), more than the 10% limit" in text
    assert "marked FAILED; nothing from it was saved" in text
    assert "RESULT: NEEDS ATTENTION" in text
    assert count(conn, "document_versions") == 20
    assert dict(conn.execute("SELECT last_seen_at FROM documents WHERE external_id = 'doc0'").fetchone()) == before
    run = conn.execute("SELECT status, error FROM scrape_runs ORDER BY run_id DESC").fetchone()
    assert run["status"] == "failed" and run["error"].startswith("ABORTED")


def test_partial_run_skips_row_count_checks(conn):
    go(conn, FakeScraper(entries(20)))
    code, text = go(conn, FakeScraper(entries(2)), partial=True)
    assert code == 0
    assert "PARTIAL" in text and "ABORTED" not in text and "not seen" not in text


# --- failures ---------------------------------------------------------------------

def test_scrape_error_is_loud_and_logged(conn):
    code, text = go(conn, FakeScraper(error=ScrapeError("SEC page 0: no table found")))

    assert code == 1
    assert "FAILED   Run #1: ScrapeError: SEC page 0: no table found" in text
    assert "STALE    Test Source: no successful run within 26h (last success: never)" in text
    run = conn.execute("SELECT status, error FROM scrape_runs").fetchone()
    assert run["status"] == "failed" and "no table found" in run["error"]


def test_unexpected_error_includes_details_and_saves_nothing(conn):
    class Broken(FakeScraper):
        def scrape(self):
            result = super().scrape()
            result.records.append(({"bad": "shape"}, {}))
            return result

    code, text = go(conn, Broken(entries(2)))
    assert code == 1 and "FAILED   Run #1: KeyError" in text and "Traceback" in text
    assert count(conn, "documents") == 0


def test_run_already_in_progress_is_reported(conn):
    db.start_run(conn, "test_src")
    code, text = go(conn, FakeScraper(entries(1)))
    assert code == 1 and "Not started" in text and "still running" in text


def test_abandoned_run_is_closed_and_reported(conn):
    db.start_run(conn, "test_src", now="2026-01-01T00:00:00.000000+00:00")
    code, text = go(conn, FakeScraper(entries(1)))
    assert code == 0 and "Closed abandoned run #1" in text


def test_enabled_source_without_scraper_fails_loudly(conn):
    lines = []
    code = run_daily.run(conn, {}, problems={"test_src": "SEC_USER_AGENT is missing"}, out=lines.append)
    text = "\n".join(lines)
    assert code == 1 and "FAILED   Not run: SEC_USER_AGENT is missing." in text and "STALE" in text


def test_ai_step_stopping_early_is_flagged(conn):
    from vigil.llm import classifier as llm

    def dead_key(version):
        raise RuntimeError("Invalid Key. Expired")

    code, text = go(conn, FakeScraper(entries(5)),
                    classifier=llm.Classifier("claude-test", "relevance_v1", dead_key))
    assert code == 1 and "AI step stopped after 3 failures in a row" in text and "Expired" in text


# --- setup helpers and command line ------------------------------------------------

def test_load_env_reads_values_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nVIGIL_T1='quoted'\nVIGIL_T2=plain\n\nVIGIL_T3=x=y\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_T2", "already set")
    for key in ("VIGIL_T1", "VIGIL_T3"):
        monkeypatch.delenv(key, raising=False)
    run_daily.load_env(env)
    assert (os.environ["VIGIL_T1"], os.environ["VIGIL_T2"], os.environ["VIGIL_T3"]) == ("quoted", "already set", "x=y")


def test_build_scrapers_requires_contact_in_user_agent(monkeypatch):
    sec = {"source_id": "sec_rulemaking", "base_url": "https://x", "enabled": True}
    other = {"source_id": "fca", "base_url": "https://y", "enabled": True}
    off = {"source_id": "esma", "base_url": "https://z", "enabled": False}

    monkeypatch.setenv("SEC_USER_AGENT", "no contact here")
    scrapers, problems = run_daily.build_scrapers([sec, other, off], None)
    assert scrapers == {} and "SEC_USER_AGENT" in problems["sec_rulemaking"]
    assert "no scraper is implemented" in problems["fca"] and "esma" not in problems

    monkeypatch.setenv("SEC_USER_AGENT", "Vigil (ops@example.test)")
    scrapers, _ = run_daily.build_scrapers([sec], 3)
    assert scrapers["sec_rulemaking"].max_pages == 3


def test_baseline_and_classify_since_cannot_be_combined():
    with pytest.raises(SystemExit):
        run_daily.parse_args(["--baseline", "--classify-since", "2026-09-01"])
    with pytest.raises(SystemExit):
        run_daily.parse_args(["--classify-since", "Sept 1"])


def test_main_end_to_end_with_a_fake_scraper(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_daily, "ENV_PATH", tmp_path / "missing.env")
    sources_file = tmp_path / "sources.yaml"
    sources_file.write_text("sources:\n  - " + "\n    ".join(f"{k}: {v}" for k, v in SOURCE.items()), encoding="utf-8")
    monkeypatch.setattr(run_daily, "SOURCES_PATH", sources_file)
    monkeypatch.setattr(run_daily, "build_scrapers", lambda sources, max_pages: ({"test_src": FakeScraper(entries(2))}, {}))

    assert run_daily.main(["--db", str(tmp_path / "vigil.db")]) == 0
    assert "2 entries: 2 new" in capsys.readouterr().out
    conn = db.connect(tmp_path / "vigil.db")
    assert db.schema_version(conn) == db.SCHEMA_VERSION and count(conn, "documents") == 2
