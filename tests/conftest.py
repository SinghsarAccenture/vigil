import pytest

from vigil.storage import db

SOURCE = {
    "source_id": "test_src",
    "name": "Test Source",
    "regulator": "TEST",
    "jurisdiction": "US",
    "scraper_type": "html",
    "base_url": "https://example.test",
    "expected_every_hours": 26,
    "enabled": True,
}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    db.sync_sources(connection, [SOURCE])
    yield connection
    connection.close()


def make_record(**overrides):
    """A normalised record in the shape every Source.normalise() returns."""
    record = {
        "external_id": "s7-01-26#33-1000proposed",
        "group_key": "s7-01-26",
        "issue_date": "2026-01-15",
        "title": "Crypto Asset Custody",
        "division": "Trading and Markets",
        "status": "Proposed Rule",
        "links": ["https://example.test/a", "https://example.test/b"],
        "file_numbers": ["S7-01-26"],
        "file_numbers_raw": " S7-01-26 ",
        "reference_numbers": ["33-1000"],
    }
    record.update(overrides)
    return record
