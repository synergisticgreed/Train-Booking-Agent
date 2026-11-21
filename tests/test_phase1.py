"""Phase 1: ingest, hybrid retrieval with precedence, rules answers, traces."""
import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "docs.json"
AS_OF = "2026-11-21T10:00:00+05:30"
H = {"X-Passenger-Id": "P001"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "nodocs"))
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("EMBEDDER", "hash")
    monkeypatch.setenv("RETRIEVAL_MIN_COS", "0.9")  # hash embedder: rely on BM25 for the relevance gate

    from app import pipeline, retrieval
    retrieval._index = None  # fresh index per test
    pipeline.reset_sessions()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        for doc in json.loads(FIXTURE.read_text(encoding="utf-8")):
            assert c.post("/ingest", json=doc).status_code == 200
        yield c


def ask(c, msg, as_of=AS_OF):
    r = c.post("/chat", headers=H, json={"session_id": "s1", "message": msg, "as_of": as_of})
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_missing_header_is_rejected(client):
    r = client.post("/chat", json={"session_id": "s1", "message": "Can I carry my pet dog?"})
    assert r.status_code == 401


def test_rules_answer_cites_source(client):
    r = ask(client, "Can I carry my pet dog?")
    assert r["answer_type"] == "answer"
    assert {"doc_id": "RUL-PET-01", "section": "4. Pets"} in r["sources"]
    assert "500" in r["answer"]
    assert set(r) == {"trace_id", "answer_type", "answer", "sources", "tools",
                      "pending_action", "handed_to_human"}


def test_circular_supersedes_rule_and_faq(client):
    r = ask(client, "What is the cancellation charge for a 2AC ticket?")
    assert r["sources"][0]["doc_id"] == "CIR-2026-07"
    assert "200" in r["answer"] and "240" not in r["answer"]
    log = client.get(f"/trace/{r['trace_id']}").json()["payload"]["retrieval"]["precedence_log"]
    reasons = {(e["overridden"].split("#")[0], e["reason"]) for e in log}
    assert ("RUL-CAN-01", "superseded_by_circular") in reasons
    assert ("FAQ-07", "official_over_faq") in reasons


def test_before_circular_effective_date_old_rule_applies(client):
    r = ask(client, "What is the cancellation charge for a 2AC ticket?", as_of="2026-10-15T10:00:00+05:30")
    assert r["sources"][0]["doc_id"] == "RUL-CAN-01"
    assert "240" in r["answer"]


def test_future_circular_is_mentioned_not_applied(client):
    r = ask(client, "Is the Tatkal charge for sleeper still Rs 150?")
    assert r["sources"][0]["doc_id"] == "RUL-TAT-01"
    assert "150" in r["answer"] and "2026-12-15" in r["answer"]
    assert any(s["doc_id"] == "CIR-2026-09" for s in r["sources"])


def test_ingest_takes_effect_immediately(client):
    assert ask(client, "Is smoking allowed on the train?")["answer_type"] == "not_found"
    client.post("/ingest", json={
        "doc_id": "RUL-SMK-01", "doc_type": "rule", "title": "Smoking", "effective_from": "2025-01-01",
        "sections": [{"section": "1. Smoking", "text": "Smoking is not allowed anywhere on Ujjwal Rail trains."}],
    })
    r = ask(client, "Is smoking allowed on the train?")
    assert r["answer_type"] == "answer" and r["sources"][0]["doc_id"] == "RUL-SMK-01"


def test_out_of_scope_refused(client):
    assert ask(client, "Book me a hotel near New Delhi station")["answer_type"] == "refusal"


def test_trace_is_replayable(client):
    r = ask(client, "Can I carry my pet dog?")
    t = client.get(f"/trace/{r['trace_id']}").json()
    p = t["payload"]
    assert p["message"] == "Can I carry my pet dog?"
    assert p["intent"]["intent"] == "policy"
    assert p["retrieval"]["hits"][0]["doc_id"] == "RUL-PET-01"
    assert p["response"]["answer"] == r["answer"]


def test_critic_rejects_invented_number():
    from app.pipeline import critic
    docs = [{"doc_id": "RUL-PET-01", "section": "4. Pets", "effective_from": "2025-04-01",
             "text": "A pet charge of Rs 500 applies per pet."}]
    good = {"answer": "It costs Rs 500.", "citations": [{"doc_id": "RUL-PET-01", "section": "4. Pets"}]}
    bad_num = {"answer": "It costs Rs 1,200.", "citations": good["citations"]}
    bad_cite = {"answer": "It costs Rs 500.", "citations": [{"doc_id": "RUL-X", "section": "1"}]}
    assert critic(good, docs)[0]
    assert not critic(bad_num, docs)[0]
    assert not critic(bad_cite, docs)[0]
