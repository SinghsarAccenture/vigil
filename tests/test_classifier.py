import sqlite3

import pytest

from conftest import make_record
from test_db import T1, T2, observe
from vigil.llm import classifier
from vigil.storage import db

REAL_ANSWER = {"relevant": True, "confidence": "medium", "reason": "Crypto custody rule.",
               "relevance_categories": ["custody"], "document_type": "proposed_rule",
               "requires_human_review": False}


def real(classify):
    return classifier.Classifier(model="claude-test", prompt_version="relevance_v1", classify=classify)


def test_placeholder_fills_in_unknown_answers(conn):
    observe(conn, make_record(), T1)
    observe(conn, make_record(external_id="second"), T1)

    summary = classifier.classify_pending(conn, classifier.PLACEHOLDER)

    assert (summary.pending, summary.succeeded, summary.failed) == (2, 2, 0)
    rows = conn.execute("SELECT * FROM classifications").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["model"] == "placeholder" and row["prompt_version"] == "placeholder"
        assert row["relevant"] is None and row["confidence"] is None
        assert row["requires_human_review"] == 1
        assert row["reason"].startswith("Placeholder") and row["error"] is None
    assert classifier.classify_pending(conn, classifier.PLACEHOLDER).pending == 0


def test_real_ai_later_replaces_placeholders(conn):
    observe(conn, make_record(), T1)
    classifier.classify_pending(conn, classifier.PLACEHOLDER)
    assert conn.execute("SELECT model, relevant FROM v_current").fetchone()["model"] == "placeholder"

    summary = classifier.classify_pending(conn, real(lambda v: REAL_ANSWER))

    assert (summary.pending, summary.succeeded) == (1, 1)
    current = conn.execute("SELECT model, relevant, confidence FROM v_current").fetchone()
    assert (current["model"], current["relevant"], current["confidence"]) == ("claude-test", 1, "medium")
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2


def test_real_model_may_not_store_unknown_relevance(conn):
    observe(conn, make_record(), T1)
    (version,) = db.pending_classifications(conn, "relevance_v1")
    with pytest.raises(sqlite3.IntegrityError):
        db.record_classification(conn, version["version_id"], "claude-test", "relevance_v1",
                                 result={**REAL_ANSWER, "relevant": None})


def test_one_failure_does_not_stop_the_step(conn):
    for i in range(3):
        observe(conn, make_record(external_id=f"doc{i}"), T1)
    calls = iter([ValueError("bad JSON"), REAL_ANSWER, REAL_ANSWER])

    def flaky(version):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    summary = classifier.classify_pending(conn, real(flaky))

    assert (summary.succeeded, summary.failed, summary.stopped_early) == (2, 1, False)
    assert len(db.pending_classifications(conn, "relevance_v1")) == 1


def test_stops_after_repeated_failures_and_retries_next_run(conn):
    for i in range(5):
        observe(conn, make_record(external_id=f"doc{i}"), T1)

    def dead_key(version):
        raise RuntimeError("Invalid Key. Expired")

    summary = classifier.classify_pending(conn, real(dead_key))

    assert (summary.failed, summary.stopped_early) == (classifier.MAX_CONSECUTIVE_FAILURES, True)
    assert "Expired" in summary.last_error
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 3
    assert len(db.pending_classifications(conn, "relevance_v1")) == 5

    retry = classifier.classify_pending(conn, real(lambda v: REAL_ANSWER))
    assert (retry.pending, retry.succeeded) == (5, 5)


def test_placeholder_respects_baseline_and_classify_since(conn):
    observe(conn, make_record(), T2, mode="baseline")
    assert classifier.classify_pending(conn, classifier.PLACEHOLDER).pending == 0
    assert classifier.classify_pending(conn, classifier.PLACEHOLDER, classify_since="2026-01-01").pending == 1
