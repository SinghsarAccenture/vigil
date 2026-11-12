from conftest import make_record
from test_run_daily import FakeScraper, go
from vigil.report import dashboard
from vigil.storage import db


def load(conn, *records):
    run_id = db.start_run(conn, "test_src")
    with conn:
        for r in records:
            db.record_observation(conn, "test_src", run_id, r, {"row": r["external_id"]}, db.utc_now())
    db.finish_run(conn, run_id, "success", rows_seen=len(records), rows_new=len(records),
                  rows_changed=0, rows_unchanged=0, rows_duplicate=0, rows_fallback_id=0)


def page(conn):
    return dashboard.render(dashboard.collect(conn), "2026-01-01 00:00 UTC")


def test_keyword_levels():
    assert dashboard.keyword_relevance("Regulation Crypto Assets")[0] == "High"
    assert dashboard.keyword_relevance("Safeguarding Advisory Client Assets")[0] == "Medium"
    assert dashboard.keyword_relevance("Adoption of Updated EDGAR Filer Manual")[0] == "Low"


def test_keywords_do_not_match_inside_other_words():
    assert dashboard.keyword_relevance("Working Together on Filings")[0] == "Low"
    assert dashboard.keyword_relevance("Electronic Submission Under the Securities Exchange Act of 1934")[0] == "Low"


def test_title_links_to_the_filing_itself(conn):
    load(conn, make_record(external_id="a", title="Crypto Asset Custody", links=["https://example.test/filing-a"]))
    assert 'href="https://example.test/filing-a"' in page(conn)


def test_falls_back_to_the_source_list_when_a_filing_has_no_link(conn):
    load(conn, make_record(external_id="a", title="No Link Rule", links=[]))
    assert 'href="https://example.test"' in page(conn)


def test_unsafe_links_are_never_used(conn):
    load(conn, make_record(external_id="a", title="Bad Link", links=["javascript:alert(1)"]))
    assert "javascript:" not in page(conn)


def test_titles_are_escaped(conn):
    load(conn, make_record(external_id="a", title="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in page(conn)


def test_a_real_ai_answer_overrides_the_keyword_guess(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"))
    version = db.get_versions(conn, db.get_document(conn, "test_src", "a")["document_id"])[0]
    db.record_classification(conn, version["version_id"], "claude-test", "relevance_v1",
                             {"relevant": False, "confidence": "high", "reason": "Not about our business"})
    html_text = page(conn)
    assert "Not about our business" in html_text and "AI (claude-test)" in html_text
    assert 'data-l="Low"' in html_text and 'data-l="High"' not in html_text


def test_placeholder_answers_fall_back_to_the_keyword_guess(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"))
    from vigil.llm import classifier as llm
    llm.classify_pending(conn, llm.PLACEHOLDER)
    html_text = page(conn)
    assert 'data-l="High"' in html_text and "Keyword guess" in html_text


def test_summary_counts_and_new_marker(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"),
         make_record(external_id="b", title="EDGAR Filer Manual"))
    html_text = page(conn)
    assert "<b>1 High</b>" in html_text and "<b>1 Low</b>" in html_text and html_text.count('class="new"') == 2


def test_run_writes_the_dashboard_and_reports_it(conn, tmp_path):
    target = tmp_path / "dash.html"
    code, text = go(conn, FakeScraper([make_record(external_id="a", title="Crypto Rule")]), dashboard_path=target)
    assert code == 0 and "Updated:" in text and "Crypto Rule" in target.read_text(encoding="utf-8")


def test_an_unwritable_dashboard_warns_but_does_not_fail_the_run(conn, tmp_path):
    code, text = go(conn, FakeScraper([make_record(external_id="a")]), dashboard_path=tmp_path / "missing" / "d.html")
    assert code == 0 and "WARNING  Dashboard not updated" in text


def test_filter_buttons_do_not_capture_the_run_button(conn):
    # Regression: a page-wide button selector let the filter code overwrite the run button's click handler.
    live = dashboard.render(dashboard.collect(conn), "now", live=True)
    assert 'id="run"' in live and 'querySelectorAll("button")' not in live
    assert 'id="filters"' in live and live.index('id="list"') < live.index('id="filters"')


def test_static_dashboard_has_no_run_button(conn):
    assert 'id="run"' not in dashboard.render(dashboard.collect(conn), "now")
