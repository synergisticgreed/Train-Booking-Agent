"""Timetable, seat and fare lookups. Pure SQL + date arithmetic."""
from datetime import date, timedelta

from app import db
from app.tools.common import runs_on

AFTER_MIDNIGHT_BEFORE = "04:00"  # departures earlier than this are flagged: "tomorrow's train" is ambiguous


def _stop(train_no: str, station_code: str) -> dict | None:
    rows = db.query("SELECT * FROM train_stops WHERE train_no = ? AND station_code = ?", (train_no, station_code))
    return rows[0] if rows else None


def _endpoints(train_no: str) -> tuple[str, str] | None:
    rows = db.query("SELECT station_code FROM train_stops WHERE train_no = ? ORDER BY stop_order", (train_no,))
    return (rows[0]["station_code"], rows[-1]["station_code"]) if rows else None


def _journey_date(train_no: str, station_code: str | None, travel_date: date) -> date:
    """Origin departure date for a passenger boarding at station_code on travel_date."""
    stop = _stop(train_no, station_code) if station_code else None
    return travel_date - timedelta(days=stop["day_offset"] if stop else 0)


def search_trains(from_code: str, to_code: str, travel_date: str, time_window: dict | None = None) -> dict:
    """Trains departing from_code on travel_date (local date at from_code) and reaching to_code."""
    d = date.fromisoformat(travel_date)
    rows = db.query(
        """SELECT t.train_no, t.name, t.runs_on,
                  a.departure AS dep, a.day_offset AS dep_off, b.arrival AS arr, b.day_offset AS arr_off
           FROM trains t
           JOIN train_stops a ON a.train_no = t.train_no AND a.station_code = ?
           JOIN train_stops b ON b.train_no = t.train_no AND b.station_code = ?
           WHERE a.stop_order < b.stop_order AND a.departure IS NOT NULL
           ORDER BY a.departure""",
        (from_code, to_code),
    )
    w = time_window or {}
    trains = []
    for r in rows:
        jd = d - timedelta(days=r["dep_off"])
        if not runs_on(r["runs_on"], jd):
            continue
        if w.get("after") and r["dep"] < w["after"]:
            continue
        if w.get("before") and r["dep"] > w["before"]:
            continue
        arr_date = jd + timedelta(days=r["arr_off"])
        trains.append({
            "train_no": r["train_no"], "name": r["name"], "journey_date": jd.isoformat(),
            "departs": f"{d.isoformat()} {r['dep']}", "arrives": f"{arr_date.isoformat()} {r['arr']}",
            "departs_after_midnight": r["dep"] < AFTER_MIDNIGHT_BEFORE,
        })
    return {"from": from_code, "to": to_code, "date": travel_date, "time_window": w or None,
            "count": len(trains), "trains": trains}


def seat_availability(train_no: str, travel_date: str, cls: str, from_code: str | None = None) -> dict:
    out = {"train_no": train_no, "class": cls, "travel_date": travel_date}
    t = db.query("SELECT runs_on, classes FROM trains WHERE train_no = ?", (train_no,))
    if not t:
        return {**out, "status": "NO_SUCH_TRAIN"}
    if cls not in [c.strip() for c in t[0]["classes"].split(",")]:
        return {**out, "status": "CLASS_NOT_ON_TRAIN"}
    jd = _journey_date(train_no, from_code, date.fromisoformat(travel_date))
    out["journey_date"] = jd.isoformat()
    if not runs_on(t[0]["runs_on"], jd):
        return {**out, "status": "NOT_RUNNING"}
    s = db.query("SELECT available, rac, waitlist FROM seats WHERE train_no = ? AND journey_date = ? AND class = ?",
                 (train_no, jd.isoformat(), cls))
    if not s:
        return {**out, "status": "NO_DATA"}
    s = s[0]
    status = "AVAILABLE" if s["available"] > 0 else "RAC" if s["rac"] > 0 else "WAITLIST"
    return {**out, **s, "status": status}


def fare(train_no: str, cls: str, from_code: str | None = None, to_code: str | None = None) -> dict:
    """Fare for a segment. Without from/to, uses the train's origin and destination."""
    if not (from_code and to_code):
        ends = _endpoints(train_no)
        if not ends:
            return {"train_no": train_no, "class": cls, "found": False}
        from_code, to_code = from_code or ends[0], to_code or ends[1]
    r = db.query("SELECT fare FROM fares WHERE train_no = ? AND class = ? AND from_code = ? AND to_code = ?",
                 (train_no, cls, from_code, to_code))
    out = {"train_no": train_no, "class": cls, "from": from_code, "to": to_code}
    return {**out, "found": True, "fare": r[0]["fare"]} if r else {**out, "found": False}
