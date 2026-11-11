"""Relevance classification of document versions.

The runner picks a classifier (model name, prompt version, and a function that
turns one document version into a result dict) and calls classify_pending().
Each attempt is stored; a failure is recorded and the run moves on.

Until the real Claude classifier is enabled, PLACEHOLDER fills in a clearly
labelled "unknown" answer. Placeholders use their own prompt version, so once
the real classifier runs, every document is still in its queue and gets a real
answer; v_current then shows the real answer because it is the newest.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from vigil.storage import db

MAX_CONSECUTIVE_FAILURES = 3

ClassifyFn = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Classifier:
    model: str
    prompt_version: str
    classify: ClassifyFn


def placeholder_answer(version: Mapping[str, Any]) -> dict[str, Any]:
    """Standard stand-in answer: relevance unknown, flagged for human review."""
    return {
        "relevant": None,
        "confidence": None,
        "reason": "Placeholder - not a real assessment. AI classification is not enabled yet.",
        "relevance_categories": [],
        "document_type": "unknown",
        "requires_human_review": True,
    }


PLACEHOLDER = Classifier(model="placeholder", prompt_version="placeholder", classify=placeholder_answer)


@dataclass
class ClassifySummary:
    model: str
    prompt_version: str
    pending: int = 0
    succeeded: int = 0
    failed: int = 0
    stopped_early: bool = False
    last_error: str | None = None


def classify_pending(conn: sqlite3.Connection, classifier: Classifier,
                     classify_since: str | None = None) -> ClassifySummary:
    """Classify every pending version, recording each success or failure.

    One failure never aborts the step. After MAX_CONSECUTIVE_FAILURES failures
    in a row (e.g. a dead API key) the step stops early so the database is not
    filled with identical errors; the rest stay pending for the next run.
    """
    pending = db.pending_classifications(conn, classifier.prompt_version, classify_since)
    summary = ClassifySummary(classifier.model, classifier.prompt_version, pending=len(pending))
    consecutive = 0
    for version in pending:
        try:
            result = classifier.classify(version)
        except Exception as exc:
            summary.failed += 1
            consecutive += 1
            summary.last_error = f"{type(exc).__name__}: {exc}"
            db.record_classification(conn, version["version_id"], classifier.model,
                                     classifier.prompt_version, error=summary.last_error)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                summary.stopped_early = True
                break
            continue
        db.record_classification(conn, version["version_id"], classifier.model,
                                 classifier.prompt_version, result=result)
        summary.succeeded += 1
        consecutive = 0
    return summary
