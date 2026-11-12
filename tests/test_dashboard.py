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


def test_summary_legend_counts_each_level(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"),
         make_record(external_id="b", title="EDGAR Filer Manual"))
    html_text = page(conn)
    assert 'dot-high"></i>High<b>1</b>' in html_text and 'dot-low"></i>Low<b>1</b>' in html_text
    assert 'dot-medium"></i>Medium<b>0</b>' in html_text


def test_hero_figure_counts_the_filings(conn):
    load(conn, make_record(external_id="a"), make_record(external_id="b"))
    assert '<div class="hero">2<span>filings tracked' in page(conn)


def test_entries_new_to_the_database_are_tagged_new(conn):
    load(conn, make_record(external_id="a"), make_record(external_id="b"))
    html_text = page(conn)
    assert html_text.count('class="tag new">NEW<') == 2
    assert html_text.count('data-new="1"') == 2
    assert '<div class="tval">2</div><div class="tlab">New in latest run</div>' in html_text


def test_entries_carried_over_from_an_earlier_run_are_not_tagged_new(conn):
    load(conn, make_record(external_id="old"))
    load(conn, make_record(external_id="old"), make_record(external_id="fresh"))
    html_text = page(conn)
    assert html_text.count('class="tag new">NEW<') == 1
    assert html_text.count('data-new="0"') == 1


def test_a_changed_filing_is_tagged_changed_with_its_version(conn):
    load(conn, make_record(external_id="a", status="Proposed Rule"))
    load(conn, make_record(external_id="a", status="Final Rule"))
    html_text = page(conn)
    assert "CHANGED &middot; v2" in html_text and 'class="tag new">NEW<' not in html_text
    assert '<div class="tval">1</div><div class="tlab">Changed in latest run</div>' in html_text


def test_each_filing_shows_its_supporting_detail(conn):
    load(conn, make_record(external_id="a", division="Trading and Markets",
                           file_numbers=["S7-01-26"], reference_numbers=["33-1000", "IA-7035"]))
    html_text = page(conn)
    assert "Trading and Markets" in html_text and "File S7-01-26" in html_text
    assert "Release 33-1000, IA-7035" in html_text and "first seen " in html_text


def test_sibling_filings_in_one_rulemaking_are_counted(conn):
    load(conn, make_record(external_id="a", group_key="s7-01-26"),
         make_record(external_id="b", group_key="s7-01-26"))
    assert "2 filings in rulemaking s7-01-26" in page(conn)


def test_a_filing_flagged_for_human_review_says_so(conn):
    load(conn, make_record(external_id="a"))
    version = db.get_versions(conn, db.get_document(conn, "test_src", "a")["document_id"])[0]
    db.record_classification(conn, version["version_id"], "claude-test", "relevance_v1",
                             {"relevant": True, "confidence": "low", "reason": "Unclear",
                              "requires_human_review": True})
    assert "NEEDS REVIEW" in page(conn)


def test_every_level_is_named_in_words_not_colour_alone(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"))
    html_text = page(conn)
    assert '<span class="b b-high">High</span>' in html_text


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


def test_placeholder_answers_do_not_tag_every_row_as_needing_review(conn):
    load(conn, make_record(external_id="a"))
    from vigil.llm import classifier as llm
    llm.classify_pending(conn, llm.PLACEHOLDER)
    assert "NEEDS REVIEW" not in page(conn)


def test_the_run_timestamp_is_trimmed_to_the_minute(conn):
    load(conn, make_record(external_id="a"))
    html_text = page(conn)
    assert ".152647" not in html_text and "+00:00" not in html_text and " UTC &middot;" in html_text


def test_a_small_share_is_never_worded_as_zero():
    # Regression: 3 High out of 1,611 rounded to "0%", which read as "none".
    assert dashboard._share(3, 1611) == "under 1%"
    assert dashboard._share(0, 1611) == "none"
    assert dashboard._share(1610, 1611) == "over 99%"
    assert dashboard._share(1611, 1611) == "100%"
    assert dashboard._share(161, 1611) == "10%"
    assert dashboard._share(0, 0) == "none"


def test_the_summary_sentence_states_the_high_count(conn):
    load(conn, make_record(external_id="a", title="Regulation Crypto Assets"),
         make_record(external_id="b", title="EDGAR Filer Manual"))
    html_text = page(conn)
    assert "the 2 tracked filings split by relevance" in html_text
    assert "<b>1</b> are rated High, 50% of the total" in html_text
