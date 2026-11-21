"""Phase 2: deterministic tools, refund cut-offs, follow-ups, tool routing, critic."""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

SEED = Path(__file__).parent / "fixtures" / "seed.sql"
AS_OF = "2026-11-21T10:00:00+05:30"


@pytest.fixture()
def client(tmp_path, monkeypatch):
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


def ask(c, msg, pid="P001", as_of=AS_OF, sid="s1"):
    r = c.post("/chat", headers={"X-Passenger-Id": pid},
               json={"session_id": sid, "message": msg, "as_of": as_of})
    assert r.status_code == 200, r.text
    return r.json()


def ist(s: str) -> datetime:
    return datetime.fromisoformat(s + "+05:30")


# ---------- refund_quote: every cut-off ----------

@pytest.mark.parametrize("as_of, refund, tier", [
    ("2026-12-03T18:29", 1840, "over_48h"),     # 48h01m before: flat Rs 200 (circular CIR-2026-07)
    ("2026-12-03T18:30", 1530, "12_to_48h"),    # exactly 48h: next tier, max(200, 25% of 2040 = 510)
    ("2026-12-03T18:31", 1530, "12_to_48h"),    # 1 min after the cut-off
    ("2026-12-05T06:29", 1530, "12_to_48h"),    # 12h01m
    ("2026-12-05T06:30", 1020, "4_to_12h"),     # exactly 12h: 50% of 2040
    ("2026-12-05T14:29", 1020, "4_to_12h"),     # 4h01m
    ("2026-12-05T14:30", 0, "within_4h"),       # exactly 4h: no refund
])
def test_refund_cutoffs(client, as_of, refund, tier):
    from app.tools import refund_quote
    q = refund_quote("P001", "UR-30418", ist(as_of))
    assert q["refund"] == refund
    assert q["per_traveller"][0]["tier"] == tier


def test_refund_uses_rule_before_circular_date(client):
    from app.tools import refund_quote
    q = refund_quote("P001", "UR-30418", ist("2026-10-20T10:00"))
    assert q["charge"] == 240 and q["refund"] == 1800
    assert q["rule_sources"] == ["RUL-CAN-01#3. Refund table"]


def test_refund_mixed_cnf_and_wl(client):
    from app.tools import refund_quote
    q = refund_quote("P002", "UR-48213", ist("2026-12-03T10:00"))
    assert [l["charge"] for l in q["per_traveller"]] == [180, 60]
    assert q["refund"] == 2260


def test_refund_other_passengers_booking_not_found(client):
    from app.tools import refund_quote
    assert refund_quote("P001", "UR-48213", ist("2026-12-03T10:00")) == {"found": False}


def test_refund_after_departure_and_cancelled(client):
    from app.tools import refund_quote
    assert refund_quote("P001", "UR-30418", ist("2026-12-05T19:00"))["reason"] == "already_departed"
    assert refund_quote("P001", "UR-30500", ist("2026-11-05T10:00"))["reason"] == "already_cancelled"


def test_ingested_circular_changes_refund_live(client):
    as_of = "2026-12-03T10:00:00+05:30"
    before = ask(client, "If I cancel my 2AC ticket now, how much will I get back?", as_of=as_of, sid="a")
    assert before["tools"][0]["output"]["refund"] == 1840
    r = client.post("/ingest", json={
        "doc_id": "CIR-2026-11", "doc_type": "circular", "title": "2AC charge cut",
        "effective_from": "2026-12-03", "topic": "cancellation",
        "sections": [{"section": "1", "text": "From 3 December 2026 the 2AC flat cancellation charge is Rs 150."}],
        "settings": [{"name": "cancel_flat_charge_2A", "value": 150, "section": "1"}],
    })
    assert r.status_code == 200
    after = ask(client, "If I cancel my 2AC ticket now, how much will I get back?", as_of=as_of, sid="b")
    assert after["tools"][0]["output"]["refund"] == 1890
    assert {"doc_id": "CIR-2026-11", "section": "1"} in after["sources"]


# ---------- train search, seats, fares ----------

def test_train_search_with_time_window(client):
    r = ask(client, "Trains from Pune to Nagpur on 5 Dec after 6 pm?")
    assert r["answer_type"] == "answer"
    out = r["tools"][0]
    assert out["tool"] == "search_trains"
    assert out["input"]["travel_date"] == "2026-12-05"
    assert [t["train_no"] for t in out["output"]["trains"]] == ["12123"]
    assert "12123" in r["answer"] and "18:30" in r["answer"]


def test_train_search_after_midnight_train(client):
    r = ask(client, "Trains from Pune to Nagpur on 4 Dec")
    trains = {t["train_no"]: t for t in r["tools"][0]["output"]["trains"]}
    assert trains["22111"]["departs"] == "2026-12-04 00:30"
    assert trains["22111"]["journey_date"] == "2026-12-03"
    assert trains["22111"]["departs_after_midnight"] is True
    assert "just after midnight" in r["answer"]


def test_station_aliases(client):
    from app.tools import resolve_station
    assert resolve_station("Bombay")["code"] == "CSMT"
    assert resolve_station("CST")["code"] == "CSMT"
    assert resolve_station("poona")["code"] == "PUNE"
    assert {o["code"] for o in resolve_station("Mumbai")["ambiguous"]} == {"CSMT", "LTT"}


def test_seat_and_fare(client):
    r = ask(client, "Is there a 3AC seat on 12123 on 5 Dec, and how much is it?")
    names = [t["tool"] for t in r["tools"]]
    assert names == ["seat_availability", "fare"]
    assert r["tools"][0]["output"]["available"] == 4
    assert "4 seats available" in r["answer"] and "Rs 1,600" in r["answer"]


# ---------- bookings and privacy ----------

def test_booking_status_own(client):
    r = ask(client, "What's the status of my ticket for 5 Dec?")
    assert r["answer_type"] == "answer"
    assert "UR-30418" in r["answer"] and "A1/12" in r["answer"]


def test_other_passengers_booking_refused_without_leaking(client):
    other = ask(client, "What's the status of booking UR-48213? It's my friend's.")
    fake = ask(client, "What's the status of booking UR-99999?", sid="s2")
    assert other["answer_type"] == fake["answer_type"] == "refusal"
    assert other["answer"] == fake["answer"]
    assert client.get("/bookings/UR-48213", headers={"X-Passenger-Id": "P001"}).status_code == 404
    assert client.get("/bookings/UR-30418", headers={"X-Passenger-Id": "P001"}).status_code == 200


def test_unknown_passenger_rejected(client):
    r = client.post("/chat", headers={"X-Passenger-Id": "P999"}, json={"session_id": "x", "message": "hi"})
    assert r.status_code == 401


def test_hinglish_refund(client):
    r = ask(client, "Kal ki train cancel karni hai, kitna paisa wapas milega?", as_of="2026-12-04T10:00:00+05:30")
    assert r["answer_type"] == "answer"
    assert r["tools"][0]["tool"] == "refund_quote"
    assert r["tools"][0]["output"]["refund"] == 1530
    assert any(s["doc_id"] == "CIR-2026-07" for s in r["sources"])


# ---------- follow-ups ----------

def test_vague_request_asks_then_answers(client):
    r1 = ask(client, "I need a train ticket from Pune")
    assert r1["answer_type"] == "clarification"
    assert "going" in r1["answer"] and "date" in r1["answer"]
    r2 = ask(client, "to Nagpur on 5 Dec")
    assert r2["answer_type"] == "answer"
    assert r2["tools"][0]["input"]["from_code"] == "PUNE"  # not asked again


def test_at_most_two_followups_in_a_row(client):
    assert ask(client, "I need a ticket to Delhi")["answer_type"] == "clarification"
    assert ask(client, "from Pune")["answer_type"] == "clarification"
    third = ask(client, "from Pune please")
    assert third["answer_type"] == "not_found"


def test_ambiguous_station_asks(client):
    r = ask(client, "Trains from Mumbai to Nagpur on 5 Dec")
    assert r["answer_type"] == "clarification"
    assert "CSMT" in r["answer"] and "LTT" in r["answer"]


# ---------- critic ----------

def test_critic_numbers():
    from app.pipeline import critic_numbers
    runs = [{"tool": "refund_quote", "input": {"booking_id": "UR-30418"},
             "output": {"refund": 1840.0, "charge": 200.0, "amount_paid": 2040.0, "departs": "2026-12-05 18:30"}}]
    assert critic_numbers("You get back Rs 1,840 (charge Rs 200). Train leaves 6:30 pm on 5 Dec.", runs)[0]
    assert not critic_numbers("You get back Rs 1,200.", runs)[0]
