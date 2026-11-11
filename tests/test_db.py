import datetime as dt
import json
import sqlite3

import pytest

from conftest import SOURCE, make_record
from vigil.storage import db

T1 = "2026-10-01T06:00:00.000000+00:00"
T2 = "2026-10-02T06:00:00.000000+00:00"
T3 = "2026-10-03T06:00:00.000000+00:00"


def observe(conn, record, at, mode="daily"):
    run_id = db.start_run(conn, "test_src", mode, now=at)
    with conn:
        result = db.record_observation(conn, "test_src", run_id, record, raw={"row": record}, observed_at=at)
    db.finish_run(conn, run_id, "success", rows_seen=1, now=at)
    return run_id, result


# --- versioning -----------------------------------------------------------

def test_new_row_creates_version_1(conn):
    run_id, result = observe(conn, make_record(), T1)

    assert result == "new"
    doc = db.get_document(conn, "test_src", "s7-01-26#33-1000proposed")
    assert doc["group_key"] == "s7-01-26"
    assert doc["first_seen_at"] == doc["last_seen_at"] == T1
    (v1,) = db.get_versions(conn, doc["document_id"])
    assert (v1["version_no"], v1["change_type"], v1["run_id"]) == (1, "new", run_id)
    assert v1["previous_version_id"] is None
    assert json.loads(v1["file_numbers_json"]) == ["S7-01-26"]
    assert v1["file_numbers_raw"] == " S7-01-26 "
    assert json.loads(v1["reference_numbers_json"]) == ["33-1000"]
    assert len(v1["content_hash"]) == 64


def test_same_row_again_only_bumps_last_seen(conn):
    observe(conn, make_record(), T1)
    _, result = observe(conn, make_record(), T2)

    assert result == "unchanged"
    doc = db.get_document(conn, "test_src", "s7-01-26#33-1000proposed")
    assert doc["first_seen_at"] == T1
    assert doc["last_seen_at"] == T2
    assert len(db.get_versions(conn, doc["document_id"])) == 1


def test_proposed_to_final_creates_linked_version_2(conn):
    observe(conn, make_record(), T1)
    run2, result = observe(conn, make_record(status="Final Rule"), T2)

    assert result == "changed"
    doc = db.get_document(conn, "test_src", "s7-01-26#33-1000proposed")
    v1, v2 = db.get_versions(conn, doc["document_id"])
    assert (v2["version_no"], v2["change_type"], v2["run_id"]) == (2, "changed", run2)
    assert v2["previous_version_id"] == v1["version_id"]
    assert v1["status"] == "Proposed Rule" and v2["status"] == "Final Rule"
    assert v1["content_hash"] != v2["content_hash"]


def test_noise_only_differences_do_not_create_a_version(conn):
    observe(conn, make_record(), T1)
    noisy = make_record(
        title="  Crypto   Asset\n Custody ",
        status="Proposed\n                 Rule",
        division=" Trading and Markets",
        links=["https://example.test/b ", "https://example.test/a"],
        file_numbers=["S7-01-26", " S7-01-26"],
        file_numbers_raw="S7-01-26\n",
    )
    _, result = observe(conn, noisy, T2)

    assert result == "unchanged"
    assert db.content_hash(noisy) == db.content_hash(make_record())


def test_fingerprint_recipe_is_locked():
    # If this fails, the fingerprint recipe changed: every stored document would
    # look "changed" on the next run. Only update deliberately, with a plan.
    assert db.content_hash(make_record()) == "9e3850b429f9c24969350b0a9175a4cdcd1c4d51b49ca04ab46399196b2ec12e"


def test_real_links_are_saved_and_page_moves_are_not_changes(conn):
    v1_link = "https://example.test/2026/01/s7-01-26#33-1000proposed"
    moved = "https://example.test/2026/09/s7-01-26#33-1000proposed"
    key = "https://example.test/s7-01-26#33-1000proposed"
    observe(conn, make_record(links=[v1_link], link_keys=[key]), T1)
    _, result = observe(conn, make_record(links=[moved], link_keys=[key]), T2)

    assert result == "unchanged"
    doc = db.get_document(conn, "test_src", "s7-01-26#33-1000proposed")
    (v1,) = db.get_versions(conn, doc["document_id"])
    assert json.loads(v1["links_json"]) == [v1_link]


def test_current_link_follows_the_groups_newest_page(conn):
    observe(conn, make_record(links=["https://example.test/2026/01/s7-01-26#33-1000proposed"]), T1)
    observe(conn, make_record(external_id="s7-01-26#33-2000final", status="Final Rule",
                              links=["https://example.test/2026/09/s7-01-26#33-2000final"]), T2)
    observe(conn, make_record(external_id="solo", group_key=None, links=["https://example.test/solo"]), T2)

    old = db.get_document(conn, "test_src", "s7-01-26#33-1000proposed")
    assert db.current_link(conn, old["document_id"]) == "https://example.test/2026/09/s7-01-26#33-1000proposed"
    solo = db.get_document(conn, "test_src", "solo")
    assert db.current_link(conn, solo["document_id"]) == "https://example.test/solo"


def test_hash_requires_canonical_dates():
    with pytest.raises(ValueError):
        db.content_hash(make_record(issue_date="Sept. 30, 2026"))
    with pytest.raises(ValueError):
        db.content_hash(make_record(issue_date="2026-9-30"))
    assert db.content_hash(make_record(issue_date=None))


def test_group_key_cannot_change_for_existing_document(conn):
    observe(conn, make_record(), T1)
    run_id = db.start_run(conn, "test_src", now=T2)
    with pytest.raises(ValueError, match="group_key changed"):
        with conn:
            db.record_observation(conn, "test_src", run_id, make_record(group_key="other"), {}, T2)


def test_failed_scrape_rolls_back_all_observations(conn):
    run_id = db.start_run(conn, "test_src", now=T1)
    with pytest.raises(RuntimeError):
        with conn:
            db.record_observation(conn, "test_src", run_id, make_record(), {}, T1)
            raise RuntimeError("page 3 failed")
    db.finish_run(conn, run_id, "failed", error="page 3 failed", now=T1)

    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert db.get_run(conn, run_id)["status"] == "failed"


# --- append-only guarantees ----------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE document_versions SET title = 'x'",
    "DELETE FROM document_versions",
    "UPDATE documents SET external_id = 'x'",
    "UPDATE documents SET first_seen_at = '2000-01-01'",
    "DELETE FROM documents",
    "UPDATE classifications SET reason = 'x'",
    "DELETE FROM classifications",
])
def test_append_only_tables_reject_edits(conn, sql):
    observe(conn, make_record(), T1)
    (version,) = db.pending_classifications(conn, "relevance_v1")
    db.record_classification(conn, version["version_id"], "m", "relevance_v1",
                             result={"relevant": True, "confidence": "low", "reason": "r"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only|only last_seen_at"):
        with conn:
            conn.execute(sql)


def test_last_seen_at_cannot_move_backwards(conn):
    observe(conn, make_record(), T2)
    with pytest.raises(sqlite3.IntegrityError, match="backwards"):
        with conn:
            conn.execute("UPDATE documents SET last_seen_at = ?", (T1,))


# --- runs and sources ------------------------------------------------------

def test_run_is_logged_and_can_only_be_closed_once(conn):
    run_id = db.start_run(conn, "test_src", now=T1)
    assert db.get_run(conn, run_id)["status"] == "running"
    db.finish_run(conn, run_id, "success", rows_seen=10, rows_new=7, rows_changed=1, rows_unchanged=2,
                  rows_duplicate=3, rows_fallback_id=1, now=T2)

    run = db.get_run(conn, run_id)
    assert (run["status"], run["rows_seen"], run["rows_duplicate"], run["rows_fallback_id"]) == ("success", 10, 3, 1)
    with pytest.raises(RuntimeError):
        db.finish_run(conn, run_id, "failed", error="again")


def test_second_run_cannot_start_while_one_is_running(conn):
    first = db.start_run(conn, "test_src", now=T1)
    with pytest.raises(db.RunInProgressError, match=f"run {first}"):
        db.start_run(conn, "test_src", now=T1)
    db.finish_run(conn, first, "success", now=T1)
    assert db.start_run(conn, "test_src", now=T2)


def test_abandoned_runs_are_closed_as_failed(conn):
    crashed = db.start_run(conn, "test_src", now=T1)
    recent_check = dt.datetime(2026, 10, 1, 9, tzinfo=dt.timezone.utc)
    assert db.close_abandoned_runs(conn, "test_src", now=recent_check) == []

    later = dt.datetime(2026, 10, 2, 6, tzinfo=dt.timezone.utc)
    closed = db.close_abandoned_runs(conn, "test_src", now=later)

    assert [r["run_id"] for r in closed] == [crashed]
    run = db.get_run(conn, crashed)
    assert run["status"] == "failed" and run["error"].startswith("abandoned")
    assert db.start_run(conn, "test_src", now=T2)


def test_empty_source_registry_is_refused(conn):
    with pytest.raises(ValueError, match="empty"):
        db.sync_sources(conn, [])
    assert conn.execute("SELECT enabled FROM sources").fetchone()[0] == 1


def test_sync_sources_updates_and_disables_removed(conn):
    other = {**SOURCE, "source_id": "other", "name": "Other"}
    db.sync_sources(conn, [SOURCE, other])
    db.sync_sources(conn, [{**SOURCE, "name": "Renamed"}])

    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM sources")}
    assert rows["test_src"]["name"] == "Renamed" and rows["test_src"]["enabled"] == 1
    assert rows["other"]["enabled"] == 0


def test_last_successful_run_ignores_failures(conn):
    ok, _ = observe(conn, make_record(), T1)
    bad = db.start_run(conn, "test_src", now=T2)
    db.finish_run(conn, bad, "failed", error="boom", now=T2)

    assert db.last_successful_run(conn, "test_src")["run_id"] == ok
    assert db.last_successful_run(conn, "test_src", before_run_id=ok) is None


# --- classifications -------------------------------------------------------

def test_classification_retried_after_failure_and_not_duplicated_after_success(conn):
    observe(conn, make_record(), T1)
    (version,) = db.pending_classifications(conn, "relevance_v1")

    db.record_classification(conn, version["version_id"], "m", "relevance_v1", error="JSON parse error")
    assert [v["version_id"] for v in db.pending_classifications(conn, "relevance_v1")] == [version["version_id"]]

    db.record_classification(conn, version["version_id"], "m", "relevance_v1",
                             result={"relevant": True, "confidence": "medium", "reason": "custody",
                                     "relevance_categories": ["custody"], "document_type": "proposed_rule",
                                     "requires_human_review": False})
    assert db.pending_classifications(conn, "relevance_v1") == []
    assert len(db.pending_classifications(conn, "relevance_v2")) == 1

    with pytest.raises(sqlite3.IntegrityError):
        db.record_classification(conn, version["version_id"], "m", "relevance_v1",
                                 result={"relevant": False, "confidence": "low", "reason": "dup"})

    row = conn.execute("SELECT * FROM v_current").fetchone()
    assert (row["relevant"], row["confidence"], row["requires_human_review"]) == (1, "medium", 0)
    assert json.loads(conn.execute(
        "SELECT output_json FROM classifications WHERE error IS NULL").fetchone()[0])["relevance_categories"] == ["custody"]


def test_only_latest_version_is_pending(conn):
    observe(conn, make_record(), T1)
    observe(conn, make_record(status="Final Rule"), T2)

    (pending,) = db.pending_classifications(conn, "relevance_v1")
    assert pending["version_no"] == 2


def test_baseline_versions_excluded_unless_classify_since(conn):
    observe(conn, make_record(), T1, mode="baseline")
    observe(conn, make_record(external_id="old", issue_date="2020-01-01"), T1, mode="baseline")

    assert db.pending_classifications(conn, "relevance_v1") == []
    since = db.pending_classifications(conn, "relevance_v1", classify_since="2026-01-01")
    assert [v["external_id"] for v in since] == ["s7-01-26#33-1000proposed"]


def test_record_classification_requires_result_xor_error(conn):
    with pytest.raises(ValueError):
        db.record_classification(conn, 1, "m", "relevance_v1")
    with pytest.raises(ValueError):
        db.record_classification(conn, 1, "m", "relevance_v1", result={"relevant": True}, error="x")


# --- monitoring --------------------------------------------------------------

def test_stale_sources_flags_source_without_recent_success(conn):
    now = dt.datetime(2026, 10, 3, 6, tzinfo=dt.timezone.utc)
    assert [s["source_id"] for s in db.stale_sources(conn, now=now)] == ["test_src"]

    observe(conn, make_record(), T1)
    assert [s["source_id"] for s in db.stale_sources(conn, now=now)] == ["test_src"]

    bad = db.start_run(conn, "test_src", now=T3)
    db.finish_run(conn, bad, "failed", error="boom", now=T3)
    assert [s["source_id"] for s in db.stale_sources(conn, now=now)] == ["test_src"]

    observe(conn, make_record(), T2)
    assert db.stale_sources(conn, now=now) == []


def test_stale_sources_ignores_disabled_sources(conn):
    db.sync_sources(conn, [{**SOURCE, "enabled": False}])
    assert db.stale_sources(conn) == []


def test_documents_not_seen_in_current_run(conn):
    observe(conn, make_record(), T1)
    observe(conn, make_record(external_id="gone", title="Withdrawn"), T1)
    run_id = db.start_run(conn, "test_src", now=T2)
    with conn:
        db.record_observation(conn, "test_src", run_id, make_record(), {}, T2)
    db.finish_run(conn, run_id, "success", now=T2)

    assert [d["external_id"] for d in db.documents_not_seen(conn, "test_src", since=T2)] == ["gone"]


def test_final_rule_in_group_with_prior_proposal_is_flagged(conn):
    observe(conn, make_record(), T1)
    run2 = db.start_run(conn, "test_src", now=T2)
    with conn:
        db.record_observation(conn, "test_src", run2, make_record(
            external_id="s7-01-26#33-2000final", status="Final Rule", issue_date="2026-09-01"), {}, T2)
        db.record_observation(conn, "test_src", run2, make_record(
            external_id="s7-99-26#33-3000final", group_key="s7-99-26", status="Final Rule"), {}, T2)
        db.record_observation(conn, "test_src", run2, make_record(
            external_id="s7-01-26#33-2001interpretive", status="Interpretive Release"), {}, T2)
    db.finish_run(conn, run2, "success", now=T2)

    (flag,) = db.final_after_proposed(conn, run2)
    assert flag["signal"] == "final_in_group"
    assert flag["external_id"] == "s7-01-26#33-2000final"
    assert flag["preceded_by"] == "s7-01-26#33-1000proposed"


def test_final_rule_dated_before_the_proposal_is_not_flagged(conn):
    observe(conn, make_record(issue_date="2026-05-01"), T1)
    run2, _ = observe(conn, make_record(external_id="s7-01-26#33-0900final", status="Final Rule",
                                        issue_date="2026-01-01"), T2)
    assert db.final_after_proposed(conn, run2) == []


def test_status_change_from_proposed_to_final_is_flagged(conn):
    observe(conn, make_record(), T1)
    run2, _ = observe(conn, make_record(status="Final Rule"), T2)

    (flag,) = db.final_after_proposed(conn, run2)
    assert (flag["signal"], flag["preceded_by"]) == ("status_changed", "Proposed Rule")
