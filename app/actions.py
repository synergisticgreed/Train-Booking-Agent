"""Safe changes: preview -> propose -> (passenger) confirm -> execute -> verify.

- preview_*  : pure checks + numbers from tools. Writes nothing.
- propose    : stores the preview as a `pending` row in `actions`. Changes no booking.
- confirm    : only via POST /actions/{id}/confirm. Owner + expiry + idempotency checks, re-runs the
               preview and refuses if anything the passenger was shown has changed, then executes
               inside one write transaction with guarded UPDATEs, then re-reads the booking (verify).
- cancel     : drops a pending action. Nothing else changes.
The LLM has no route to confirm(); the chat pipeline can only call preview_* and propose().
"""
import json
import random
import uuid
from datetime import datetime, timedelta

from app import db, tools
from app.tools.common import departure_at, resolve_station
from app.tools.trains import _journey_date

ACTIVE = ("CONFIRMED", "RAC", "WAITLISTED")
DEFAULT_TTL_MIN = 15
COACH = {"SL": "S1", "3A": "B1", "2A": "A1", "1A": "H1", "CC": "C1"}
STATUS_CODE = {"CONFIRMED": "CNF", "RAC": "RAC", "WAITLISTED": "WL"}


class ActionError(Exception):
    def __init__(self, http_status: int, code: str, detail: dict | None = None):
        super().__init__(code)
        self.http_status, self.code, self.detail = http_status, code, detail or {}


def _ok(preview: dict, runs: list) -> dict:
    return {"allowed": True, "preview": preview, "tool_runs": runs}


def _no(reason: str, runs: list, **extra) -> dict:
    return {"allowed": False, "reason": reason, "tool_runs": runs, **extra}


def _run(runs: list, name: str, fn, **kw) -> dict:
    out = fn(**kw)
    runs.append({"tool": name, "input": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in kw.items()},
                 "output": out})
    return out


# ---------- previews (no writes) ----------

def preview_cancel(pid: str, booking_id: str, as_of: datetime) -> dict:
    runs: list = []
    q = _run(runs, "refund_quote", tools.refund_quote, passenger_id=pid, booking_id=booking_id, as_of=as_of)
    if not q.get("found"):
        return _no("not_found", runs)
    if q.get("error"):
        return _no("rule_value_missing", runs)
    if not q.get("cancellable"):
        return _no(q["reason"], runs)
    b = tools.find_bookings(pid, booking_id)[0]
    preview = {"booking_id": booking_id, "train_no": b["train_no"], "journey_date": b["journey_date"],
               "from": b["from_code"], "to": b["to_code"], "class": b["class"],
               "travellers": len(q["per_traveller"]), "amount_paid": q["amount_paid"],
               "charge": q["charge"], "refund": q["refund"], "departs": q["departs"],
               "hours_before_departure": q["hours_before_departure"], "rule_sources": q["rule_sources"]}
    return _ok(preview, runs)


def preview_change_boarding(pid: str, booking_id: str, new_boarding: str, as_of: datetime) -> dict:
    runs: list = []
    rows = tools.find_bookings(pid, booking_id)
    if not rows:
        return _no("not_found", runs)
    b = rows[0]
    if b["status"] not in ACTIVE:
        return _no(f"booking_{b['status'].lower()}", runs)
    if new_boarding == b["from_code"]:
        return _no("same_station", runs)
    stops = {r["station_code"]: r for r in db.query(
        "SELECT station_code, stop_order, departure FROM train_stops WHERE train_no = ?", (b["train_no"],))}
    new, dest = stops.get(new_boarding), stops.get(b["to_code"])
    if not new or not new["departure"] or new["stop_order"] >= dest["stop_order"]:
        return _no("station_not_on_route_before_destination", runs)

    cutoff = db.get_setting("boarding_change_cutoff_hours", as_of.date().isoformat())
    if cutoff is None:
        return _no("rule_value_missing", runs)
    old_dep = departure_at(b["train_no"], b["from_code"], b["journey_date"])
    hours_left = (old_dep - as_of).total_seconds() / 3600
    check = {"booking_id": booking_id, "current_boarding": b["from_code"], "departs": old_dep.strftime("%Y-%m-%d %H:%M"),
             "hours_before_departure": round(hours_left, 2), "cutoff_hours": float(cutoff[0]),
             "rule_source": cutoff[1]}
    runs.append({"tool": "boarding_change_check", "input": {"booking_id": booking_id, "new_boarding": new_boarding},
                 "output": check})
    if not hours_left > float(cutoff[0]):  # "more than N hours before departure" is strict
        return _no("past_boarding_change_cutoff", runs, check=check)

    new_dep = departure_at(b["train_no"], new_boarding, b["journey_date"])
    preview = {"booking_id": booking_id, "train_no": b["train_no"], "journey_date": b["journey_date"],
               "old_boarding": b["from_code"], "new_boarding": new_boarding,
               "new_boarding_name": resolve_station(new_boarding).get("name", new_boarding),
               "new_departs": new_dep.strftime("%Y-%m-%d %H:%M"), "fare_change": 0,
               "rule_sources": [cutoff[1]] if cutoff[1] else []}
    return _ok(preview, runs)


def preview_book(pid: str, train_no: str, travel_date: str, cls: str, from_code: str, to_code: str,
                 passengers: int, as_of: datetime) -> dict:
    runs: list = []
    s = _run(runs, "seat_availability", tools.seat_availability, train_no=train_no, travel_date=travel_date,
             cls=cls, from_code=from_code)
    if s["status"] in ("NO_SUCH_TRAIN", "CLASS_NOT_ON_TRAIN", "NOT_RUNNING", "NO_DATA"):
        return _no(s["status"].lower(), runs)
    f = _run(runs, "fare", tools.fare, train_no=train_no, cls=cls, from_code=from_code, to_code=to_code)
    if not f.get("found"):
        return _no("no_fare_for_segment", runs)
    dep = departure_at(train_no, from_code, s["journey_date"])
    if dep is None or dep <= as_of:
        return _no("already_departed", runs)
    expected = ("CONFIRMED" if s["available"] >= passengers else
                "RAC" if s["rac"] >= passengers else "WAITLISTED")
    preview = {"train_no": train_no, "journey_date": s["journey_date"], "travel_date": travel_date,
               "departs": dep.strftime("%Y-%m-%d %H:%M"), "from": from_code, "to": to_code, "class": cls,
               "passengers": passengers, "fare_per_person": f["fare"], "total": f["fare"] * passengers,
               "expected_status": expected}
    return _ok(preview, runs)


# ---------- propose / confirm / cancel ----------

def _ttl_minutes(as_of: datetime) -> int:
    v = db.get_setting("action_ttl_minutes", as_of.date().isoformat())
    return int(float(v[0])) if v else DEFAULT_TTL_MIN


def propose(pid: str, action_type: str, booking_id: str | None, preview: dict, as_of: datetime) -> dict:
    action_id = "ACT-" + uuid.uuid4().hex[:8].upper()
    ttl = _ttl_minutes(as_of)
    expires = as_of + timedelta(minutes=ttl)
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO actions (action_id, passenger_id, booking_id, action_type, status, created_at,
                                    expires_at, payload) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (action_id, pid, booking_id, action_type, as_of.isoformat(), expires.isoformat(), json.dumps(preview)))
    return {"action_id": action_id, "type": action_type, "booking_id": booking_id,
            "expires_at": expires.isoformat(), "ttl_minutes": ttl}


def get_action(pid: str, action_id: str) -> dict:
    rows = db.query("SELECT * FROM actions WHERE action_id = ?", (action_id,))
    if not rows or rows[0]["passenger_id"] != pid:
        raise ActionError(404, "action_not_found")  # same for "not yours" and "doesn't exist"
    a = rows[0]
    a["payload"] = json.loads(a["payload"] or "{}")
    a["result"] = json.loads(a["result"]) if a["result"] else None
    return a


def _set_status(action_id: str, status: str, result: dict | None = None, only_if: str = "pending") -> None:
    with db.connect() as conn:
        conn.execute("UPDATE actions SET status = ?, result = COALESCE(?, result) WHERE action_id = ? AND status = ?",
                     (status, json.dumps(result) if result else None, action_id, only_if))


# Fields that must be unchanged between preview and confirm, per action type.
_MUST_MATCH = {
    "cancel_booking": ("refund", "charge"),
    "change_boarding": ("new_boarding", "new_departs"),
    "book_ticket": ("total", "expected_status"),
}


def _repreview(a: dict, as_of: datetime) -> dict:
    p = a["payload"]
    if a["action_type"] == "cancel_booking":
        return preview_cancel(a["passenger_id"], a["booking_id"], as_of)
    if a["action_type"] == "change_boarding":
        return preview_change_boarding(a["passenger_id"], a["booking_id"], p["new_boarding"], as_of)
    return preview_book(a["passenger_id"], p["train_no"], p["travel_date"], p["class"], p["from"], p["to"],
                        p["passengers"], as_of)


def confirm(pid: str, action_id: str, as_of: datetime) -> dict:
    """Returns {"action", "result", "already_done"}; raises ActionError (404 / 409)."""
    a = get_action(pid, action_id)
    if a["status"] == "done":
        return {"action": a, "result": a["result"], "already_done": True}  # idempotent replay
    if a["status"] != "pending":
        raise ActionError(409, f"action_{a['status']}", {"result": a["result"]})
    if as_of > datetime.fromisoformat(a["expires_at"]):
        _set_status(action_id, "expired")
        raise ActionError(409, "action_expired")

    fresh = _repreview(a, as_of)
    keys = _MUST_MATCH[a["action_type"]]
    if not fresh["allowed"] or any(fresh["preview"].get(k) != a["payload"].get(k) for k in keys):
        result = {"reason": fresh.get("reason", "preview_changed"),
                  "now": {k: fresh.get("preview", {}).get(k) for k in keys}}
        _set_status(action_id, "failed", result)
        raise ActionError(409, "preview_no_longer_valid", result)

    try:
        with db.transaction() as conn:
            claimed = conn.execute("UPDATE actions SET status = 'executing' WHERE action_id = ? AND status = 'pending'",
                                   (action_id,)).rowcount
            if claimed != 1:  # a concurrent confirm got here first
                raise ActionError(409, "action_in_progress")
            result = _EXECUTE[a["action_type"]](conn, a, as_of)
            conn.execute("UPDATE actions SET status = 'done', result = ?, booking_id = ? WHERE action_id = ?",
                         (json.dumps(result), result["booking_id"], action_id))
    except _GuardFailed as e:
        _set_status(action_id, "failed", {"reason": str(e)})
        raise ActionError(409, "booking_changed", {"reason": str(e)})

    result["verification"] = verify(pid, a["action_type"], result)
    with db.connect() as conn:
        conn.execute("UPDATE actions SET result = ? WHERE action_id = ?", (json.dumps(result), action_id))
    return {"action": get_action(pid, action_id), "result": result, "already_done": False}


def cancel(pid: str, action_id: str) -> dict:
    a = get_action(pid, action_id)
    if a["status"] in ("cancelled", "expired"):
        return {"action": a, "already": True}
    if a["status"] != "pending":
        raise ActionError(409, f"action_{a['status']}")
    _set_status(action_id, "cancelled")
    return {"action": get_action(pid, action_id), "already": False}


# ---------- execution (inside the write transaction; every UPDATE is guarded) ----------

class _GuardFailed(Exception):
    pass


def _exec_cancel(conn, a: dict, as_of: datetime) -> dict:
    p = a["payload"]
    n = conn.execute(
        f"UPDATE bookings SET status = 'CANCELLED' WHERE booking_id = ? AND passenger_id = ? "
        f"AND status IN ({','.join('?' * len(ACTIVE))})", (a["booking_id"], a["passenger_id"], *ACTIVE)).rowcount
    if n != 1:
        raise _GuardFailed("booking_not_active")
    cnf = conn.execute("SELECT COUNT(*) FROM booking_travellers WHERE booking_id = ? AND status = 'CNF'",
                       (a["booking_id"],)).fetchone()[0]
    if cnf:  # release confirmed berths back to the pool
        conn.execute("UPDATE seats SET available = available + ? WHERE train_no = ? AND journey_date = ? AND class = ?",
                     (cnf, p["train_no"], p["journey_date"], p["class"]))
    return {"booking_id": a["booking_id"], "status": "CANCELLED", "refund": p["refund"], "charge": p["charge"]}


def _exec_change_boarding(conn, a: dict, as_of: datetime) -> dict:
    p = a["payload"]
    n = conn.execute(
        f"UPDATE bookings SET from_code = ? WHERE booking_id = ? AND passenger_id = ? AND from_code = ? "
        f"AND status IN ({','.join('?' * len(ACTIVE))})",
        (p["new_boarding"], a["booking_id"], a["passenger_id"], p["old_boarding"], *ACTIVE)).rowcount
    if n != 1:
        raise _GuardFailed("booking_changed")
    return {"booking_id": a["booking_id"], "from": p["new_boarding"], "departs": p["new_departs"]}


def _exec_book(conn, a: dict, as_of: datetime) -> dict:
    p = a["payload"]
    n, status = p["passengers"], p["expected_status"]
    key = (p["train_no"], p["journey_date"], p["class"])
    if status == "CONFIRMED":
        row = conn.execute("SELECT available FROM seats WHERE train_no = ? AND journey_date = ? AND class = ?", key).fetchone()
        ok = conn.execute("UPDATE seats SET available = available - ? WHERE train_no = ? AND journey_date = ? "
                          "AND class = ? AND available >= ?", (n, *key, n)).rowcount
        first_berth = (row["available"] if row else 0) - n + 1
    elif status == "RAC":
        ok = conn.execute("UPDATE seats SET rac = rac - ? WHERE train_no = ? AND journey_date = ? AND class = ? "
                          "AND rac >= ?", (n, *key, n)).rowcount
    else:
        ok = conn.execute("UPDATE seats SET waitlist = waitlist + ? WHERE train_no = ? AND journey_date = ? "
                          "AND class = ?", (n, *key)).rowcount
    if ok != 1:
        raise _GuardFailed("seats_changed")

    while True:
        booking_id = f"UR-{random.randint(60000, 99999)}"
        if not conn.execute("SELECT 1 FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone():
            break
    conn.execute("INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (booking_id, a["passenger_id"], p["train_no"], p["journey_date"], p["from"], p["to"], p["class"],
                  p["total"], status, as_of.isoformat()))
    name = conn.execute("SELECT name FROM passengers WHERE passenger_id = ?", (a["passenger_id"],)).fetchone()
    name = name["name"] if name else a["passenger_id"]
    for i in range(n):
        coach = COACH.get(p["class"]) if status == "CONFIRMED" else None
        berth = str(first_berth + i) if status == "CONFIRMED" else None
        conn.execute("INSERT INTO booking_travellers VALUES (?, ?, ?, ?, ?, ?)",
                     (booking_id, name if i == 0 else f"{name} (co-traveller {i})", None, STATUS_CODE[status],
                      coach, berth))
    return {"booking_id": booking_id, "status": status, "amount_paid": p["total"], "passengers": n}


_EXECUTE = {"cancel_booking": _exec_cancel, "change_boarding": _exec_change_boarding, "book_ticket": _exec_book}


# ---------- verification (fresh read after commit) ----------

def verify(pid: str, action_type: str, result: dict) -> dict:
    out = tools.booking_status(pid, booking_id=result["booking_id"])
    b = out["bookings"][0] if out["bookings"] else None
    if action_type == "cancel_booking":
        ok = bool(b) and b["status"] == "CANCELLED"
    elif action_type == "change_boarding":
        ok = bool(b) and b["from"] == result["from"]
    else:
        ok = bool(b) and b["status"] == result["status"] and len(b["travellers"]) == result["passengers"]
    return {"verified": ok, "booking": b}
