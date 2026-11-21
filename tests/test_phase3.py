"""Phase 3: propose -> confirm -> execute -> verify; idempotency, ownership, expiry, drift, cancel."""
import pytest

from app import db

T0 = "2026-12-03T10:00:00+05:30"      # 56.5 h before UR-30418 leaves Pune (2026-12-05 18:30)


def ask(c, msg, pid="P001", as_of=T0, sid="s1"):
    r = c.post("/chat", headers={"X-Passenger-Id": pid}, json={"session_id": sid, "message": msg, "as_of": as_of})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, action_id, pid="P001", as_of="2026-12-03T10:05:00+05:30"):
    return c.post(f"/actions/{action_id}/confirm", headers={"X-Passenger-Id": pid}, json={"as_of": as_of})


def booking(booking_id):
    return db.query("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))[0]


def seats(train, jd, cls):
    return db.query("SELECT * FROM seats WHERE train_no = ? AND journey_date = ? AND class = ?", (train, jd, cls))[0]


# ---------- cancel: preview -> confirm -> change -> check ----------

def test_cancel_full_flow(seeded):
    r = ask(seeded, "Cancel my ticket for 5 Dec")
    assert r["answer_type"] == "confirm_needed"
    pa = r["pending_action"]
    assert set(pa) == {"action_id", "type", "booking_id", "expires_at"}
    assert pa["type"] == "cancel_booking" and pa["booking_id"] == "UR-30418"
    assert pa["expires_at"].startswith("2026-12-03T10:15")
    assert "Rs 1,840" in r["answer"] and "Nothing has changed yet" in r["answer"]
    assert any(t["tool"] == "refund_quote" and t["output"]["refund"] == 1840 for t in r["tools"])
    assert booking("UR-30418")["status"] == "CONFIRMED"          # proposing changes nothing
    assert seats("12123", "2026-12-05", "2A")["available"] == 0

    c1 = confirm(seeded, pa["action_id"])
    assert c1.status_code == 200
    body = c1.json()
    assert body["answer_type"] == "action_done"
    assert body["tools"][0]["output"]["verified"] is True
    assert booking("UR-30418")["status"] == "CANCELLED"
    assert seats("12123", "2026-12-05", "2A")["available"] == 1   # berth released

    # Trace of the confirm is replayable.
    t = seeded.get(f"/trace/{body['trace_id']}").json()["payload"]
    assert t["action_confirmed"]["result"]["refund"] == 1840


def test_confirm_twice_is_idempotent(seeded):
    pa = ask(seeded, "Cancel my ticket for 5 Dec")["pending_action"]
    first = confirm(seeded, pa["action_id"]).json()
    second = confirm(seeded, pa["action_id"], as_of="2026-12-03T10:06:00+05:30")
    assert second.status_code == 200
    assert second.json()["answer_type"] == "action_done"
    assert "already confirmed" in second.json()["answer"]
    assert seats("12123", "2026-12-05", "2A")["available"] == 1   # not released twice
    assert first["answer_type"] == "action_done"


def test_only_owner_can_confirm(seeded):
    pa = ask(seeded, "Cancel my ticket for 5 Dec")["pending_action"]
    r = confirm(seeded, pa["action_id"], pid="P002")
    assert r.status_code == 404
    unknown = confirm(seeded, "ACT-DOESNOTX", pid="P002")
    assert unknown.status_code == 404 and unknown.json()["answer"] == r.json()["answer"]
    assert booking("UR-30418")["status"] == "CONFIRMED"


def test_cancel_endpoint_drops_change(seeded):
    pa = ask(seeded, "Cancel my ticket for 5 Dec")["pending_action"]
    r = seeded.post(f"/actions/{pa['action_id']}/cancel", headers={"X-Passenger-Id": "P001"})
    assert r.status_code == 200 and "Nothing was changed" in r.json()["answer"]
    assert booking("UR-30418")["status"] == "CONFIRMED"
    assert confirm(seeded, pa["action_id"]).status_code == 409   # can't confirm a dropped change
    assert booking("UR-30418")["status"] == "CONFIRMED"


def test_expired_action_cannot_be_confirmed(seeded):
    pa = ask(seeded, "Cancel my ticket for 5 Dec")["pending_action"]
    r = confirm(seeded, pa["action_id"], as_of="2026-12-03T10:16:00+05:30")
    assert r.status_code == 409 and "expired" in r.json()["answer"]
    assert booking("UR-30418")["status"] == "CONFIRMED"


def test_refund_drift_between_preview_and_confirm_is_refused(seeded):
    # Preview at 48h10m (Rs 1,840), confirm at 47h59m: the 48 h cut-off has passed (Rs 1,530).
    pa = ask(seeded, "Cancel my ticket for 5 Dec", as_of="2026-12-03T18:20:00+05:30")["pending_action"]
    r = confirm(seeded, pa["action_id"], as_of="2026-12-03T18:31:00+05:30")
    assert r.status_code == 409 and "changed since the preview" in r.json()["answer"]
    assert booking("UR-30418")["status"] == "CONFIRMED"


def test_yes_in_chat_does_not_confirm(seeded):
    pa = ask(seeded, "Cancel my ticket for 5 Dec")["pending_action"]
    r = ask(seeded, "yes, confirm it")
    assert r["answer_type"] == "confirm_needed"
    assert r["pending_action"]["action_id"] == pa["action_id"]
    assert booking("UR-30418")["status"] == "CONFIRMED"


def test_cancel_not_allowed_cases(seeded):
    assert ask(seeded, "Cancel booking UR-30500")["answer_type"] == "refusal"            # already cancelled
    other = ask(seeded, "Cancel booking UR-48213", sid="s2")                              # someone else's
    assert other["answer_type"] == "refusal" and other["pending_action"] is None
    assert booking("UR-48213")["status"] == "CONFIRMED"
    gone = ask(seeded, "Cancel my ticket for 5 Dec", as_of="2026-12-05T19:00:00+05:30", sid="s3")
    assert gone["answer_type"] == "not_found" or gone["answer_type"] == "refusal"


# ---------- book ----------

def test_book_flow(seeded):
    r = ask(seeded, "Book a 3A ticket on 12123 from Pune to Nagpur on 5 Dec", as_of="2026-11-21T10:00:00+05:30")
    assert r["answer_type"] == "confirm_needed", r["answer"]
    pa = r["pending_action"]
    assert pa["type"] == "book_ticket" and pa["booking_id"] is None
    assert "Rs 1,250" in r["answer"] and "confirmed berths" in r["answer"]
    before = db.query("SELECT COUNT(*) n FROM bookings WHERE passenger_id = 'P001'")[0]["n"]

    c1 = confirm(seeded, pa["action_id"], as_of="2026-11-21T10:01:00+05:30").json()
    c2 = confirm(seeded, pa["action_id"], as_of="2026-11-21T10:02:00+05:30").json()
    assert c1["answer_type"] == c2["answer_type"] == "action_done"
    after = db.query("SELECT * FROM bookings WHERE passenger_id = 'P001'")
    assert len(after) == before + 1                                # booked exactly once
    new = [b for b in after if b["booking_id"] not in ("UR-30418", "UR-30500")][0]
    assert new["status"] == "CONFIRMED" and new["amount_paid"] == 1250
    assert seats("12123", "2026-12-05", "3A")["available"] == 3
    assert seeded.get(f"/bookings/{new['booking_id']}", headers={"X-Passenger-Id": "P001"}).status_code == 200


def test_book_when_full_goes_waitlisted(seeded):
    r = ask(seeded, "Book a sleeper ticket on 12123 from Pune to Nagpur on 5 Dec", as_of="2026-11-21T10:00:00+05:30")
    assert r["answer_type"] == "confirm_needed"
    assert "waitlisted" in r["answer"]


def test_book_asks_for_missing_details(seeded):
    r = ask(seeded, "Book a ticket on 12123", as_of="2026-11-21T10:00:00+05:30")
    assert r["answer_type"] == "clarification"
    assert r["pending_action"] is None


# ---------- change boarding station ----------

def test_change_boarding_flow(seeded):
    r = ask(seeded, "Change my boarding station to Kalyan", as_of="2026-12-02T10:00:00+05:30")
    assert r["answer_type"] == "confirm_needed", r["answer"]
    pa = r["pending_action"]
    assert pa["type"] == "change_boarding"
    assert booking("UR-30418")["from_code"] == "PUNE"
    c = confirm(seeded, pa["action_id"], as_of="2026-12-02T10:05:00+05:30").json()
    assert c["answer_type"] == "action_done"
    assert booking("UR-30418")["from_code"] == "KYN"


def test_change_boarding_after_cutoff_refused(seeded):
    r = ask(seeded, "Change my boarding station to Kalyan", as_of="2026-12-05T10:00:00+05:30")
    assert r["answer_type"] == "refusal" and "cut-off" in r["answer"]
    assert r["pending_action"] is None
    assert booking("UR-30418")["from_code"] == "PUNE"


def test_change_boarding_to_station_not_on_route(seeded):
    r = ask(seeded, "Change my boarding station to New Delhi", as_of="2026-12-02T10:00:00+05:30")
    assert r["answer_type"] == "refusal"


def test_book_a_train_from_the_list_just_shown(seeded):
    """Search -> "please book that train" -> pick from the list -> class -> preview -> Confirm -> new booking."""
    t = "2026-11-21T10:00:00+05:30"
    r = ask(seeded, "Trains from Pune to Nagpur on 5 Dec", as_of=t, sid="bk")
    assert r["answer_type"] == "answer" and len(r["tools"][0]["output"]["trains"]) == 2
    r = ask(seeded, "please book that train", as_of=t, sid="bk")
    assert r["answer_type"] == "clarification" and "Which train" in r["answer"]
    assert ask(seeded, "what details are needed", as_of=t, sid="bk")["answer"] == r["answer"]  # repeats, no hand-over
    r = ask(seeded, "the second one", as_of=t, sid="bk")   # 12123 (the fixture has seat data for it)
    assert r["answer_type"] == "clarification" and "Which class" in r["answer"]
    r = ask(seeded, "SL", as_of=t, sid="bk")
    assert r["answer_type"] == "confirm_needed" and r["pending_action"]["type"] == "book_ticket"
    before = {b["booking_id"] for b in db.query("SELECT booking_id FROM bookings")}
    done = confirm(seeded, r["pending_action"]["action_id"], as_of="2026-11-21T10:01:00+05:30").json()
    assert done["answer_type"] == "action_done"
    new = [b for b in db.query("SELECT * FROM bookings") if b["booking_id"] not in before]
    assert len(new) == 1 and new[0]["passenger_id"] == "P001" and new[0]["booking_id"] in done["answer"]


def test_single_listed_train_is_that_train(seeded):
    t = "2026-11-21T10:00:00+05:30"
    ask(seeded, "Trains from Pune to Nagpur on 5 Dec after 6 pm", as_of=t, sid="one")
    r = ask(seeded, "please book that train", as_of=t, sid="one")
    assert r["answer_type"] == "clarification" and "12123" in r["answer"] and "Which class" in r["answer"]
