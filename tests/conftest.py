import sqlite3
from pathlib import Path

import pytest

SEED = Path(__file__).parent / "fixtures" / "seed.sql"


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """TestClient over a fresh DB loaded with tests/fixtures/seed.sql, mock LLM, offline embedder."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "nodocs"))
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("EMBEDDER", "hash")
    monkeypatch.setenv("RETRIEVAL_MIN_COS", "0.9")

    from app import db, pipeline, retrieval
    retrieval._index = None
    pipeline.reset_sessions()
    db.init_db()
    conn = sqlite3.connect(db_file)
    conn.executescript(SEED.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
