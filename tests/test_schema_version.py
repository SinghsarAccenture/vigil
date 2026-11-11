import pytest

from conftest import SOURCE
from vigil.storage import db


def fresh(tmp_path):
    conn = db.connect(tmp_path / "vigil.db")
    db.init_db(conn)
    return conn


def test_new_database_is_stamped_with_current_version(tmp_path):
    conn = fresh(tmp_path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_reopening_a_current_database_changes_nothing(tmp_path):
    conn = fresh(tmp_path)
    db.sync_sources(conn, [SOURCE])
    conn.close()

    conn = db.connect(tmp_path / "vigil.db")
    db.init_db(conn)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_older_database_is_upgraded_in_place_keeping_data(tmp_path, monkeypatch):
    conn = fresh(tmp_path)
    db.sync_sources(conn, [SOURCE])

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    monkeypatch.setattr(db, "MIGRATIONS", {db.SCHEMA_VERSION: "ALTER TABLE sources ADD COLUMN notes TEXT"})
    db.init_db(conn)

    assert db.schema_version(conn) == db.SCHEMA_VERSION
    row = conn.execute("SELECT name, notes FROM sources").fetchone()
    assert (row["name"], row["notes"]) == ("Test Source", None)


def test_failed_upgrade_leaves_database_untouched(tmp_path, monkeypatch):
    conn = fresh(tmp_path)
    before = db.schema_version(conn)

    monkeypatch.setattr(db, "SCHEMA_VERSION", before + 1)
    monkeypatch.setattr(db, "MIGRATIONS", {
        before + 1: "ALTER TABLE sources ADD COLUMN notes TEXT; ALTER TABLE no_such_table ADD COLUMN x TEXT"})
    with pytest.raises(db.SchemaVersionError, match="nothing changed"):
        db.init_db(conn)

    assert db.schema_version(conn) == before
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(sources)")]
    assert "notes" not in columns


def test_missing_upgrade_step_stops(tmp_path, monkeypatch):
    conn = fresh(tmp_path)
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    with pytest.raises(db.SchemaVersionError, match="no upgrade step"):
        db.init_db(conn)


def test_database_newer_than_code_stops(tmp_path):
    conn = fresh(tmp_path)
    with conn:
        conn.execute("UPDATE schema_info SET version = ?", (db.SCHEMA_VERSION + 1,))
    with pytest.raises(db.SchemaVersionError, match="update the code"):
        db.init_db(conn)


def test_unstamped_database_with_tables_stops(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE documents (document_id INTEGER PRIMARY KEY)")
    with pytest.raises(db.SchemaVersionError, match="no version stamp"):
        db.init_db(conn)
