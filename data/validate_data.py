"""Check the generated Ujjwal Rail data. Exit code 0 = all checks pass, 1 = something is wrong.

    python data/validate_data.py
"""
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
failures: list[str] = []
passes: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    (passes if ok else failures).append(what + (f": {detail}" if detail and not ok else ""))


def main() -> int:
    conn = sqlite3.connect(db.db_path())
    conn.row_factory = sqlite3.Row
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p).fetchall()]  # noqa: E731
    meta = json.loads((ROOT / "data" / "generation_meta.json").read_text(encoding="utf-8"))
    start = date.fromisoformat(meta["start"])

    stations = {r["station_code"]: r for r in q("SELECT * FROM stations")}
    trains = {r["train_no"]: r for r in q("SELECT * FROM trains")}
    stops = defaultdict(list)
    for r in q("SELECT * FROM train_stops ORDER BY train_no, stop_order"):
        stops[r["train_no"]].append(r)
    passengers = {r["passenger_id"]: r for r in q("SELECT * FROM passengers")}
    bookings = q("SELECT * FROM bookings")
    trav = defaultdict(list)
    for r in q("SELECT * FROM booking_travellers"):
        trav[r["booking_id"]].append(r)
    fares = {(r["train_no"], r["class"], r["from_code"], r["to_code"]): r["fare"] for r in q("SELECT * FROM fares")}
    seats = {(r["train_no"], r["journey_date"], r["class"]): r for r in q("SELECT * FROM seats")}
    settings = q("SELECT * FROM settings")
    docs = q("SELECT * FROM documents")

    # ---------- minimum counts (brief section 4) ----------
    check(len(stations) >= 15, "at least 15 stations", str(len(stations)))
    check(len(trains) >= 25, "at least 25 trains", str(len(trains)))
    check(len(passengers) >= 25, "at least 25 passengers", str(len(passengers)))
    check(len(bookings) >= 50, "at least 50 bookings", str(len(bookings)))
    statuses = Counter(b["status"] for b in bookings)
    for s in ("CONFIRMED", "RAC", "WAITLISTED", "CANCELLED", "TRAIN_CANCELLED"):
        check(statuses[s] > 0, f"bookings in status {s}", str(dict(statuses)))
    check(len({p["preferred_language"] for p in passengers.values()}) >= 3, "passengers use en, hi and hinglish")
    check(len({s["city"] for s in stations.values()}) >= 8, "stations in at least 8 cities")

    # ---------- timetable ----------
    def runs(t, d: date) -> bool:
        ro = trains[t]["runs_on"]
        return ro == "Daily" or DAYS[d.weekday()] in ro.split(",")

    def abs_minutes(s: dict, key: str) -> int:
        h, m = map(int, s[key].split(":"))
        return s["day_offset"] * 1440 + h * 60 + m

    bad_route = []
    for t, ss in stops.items():
        if t not in trains or len(ss) < 2 or ss[0]["arrival"] or ss[-1]["departure"]:
            bad_route.append(t)
            continue
        prev = -1
        for s in ss:
            if s["station_code"] not in stations:
                bad_route.append(f"{t}:{s['station_code']}")
            for k in ("arrival", "departure"):
                if s[k]:
                    m = abs_minutes(s, k)
                    if m < prev:
                        bad_route.append(f"{t}@{s['station_code']}")
                    prev = m
    check(not bad_route, "every train has a valid route with times moving forward", str(bad_route[:5]))
    check(all(len(stops[t]) >= 2 for t in trains), "every train has stops")
    for t, r in trains.items():
        for c in r["classes"].split(","):
            first, last = stops[t][0]["station_code"], stops[t][-1]["station_code"]
            if (t, c, first, last) not in fares:
                failures.append(f"missing end-to-end fare {t} {c}")
    check(sum(r["runs_on"] != "Daily" for r in trains.values()) >= 6, "at least 6 trains not daily")
    check(any(t[:2] in ("01", "02") for t in trains), "festival special trains exist")

    # ---------- bookings ----------
    problems = []
    for b in bookings:
        t = b["train_no"]
        if t not in trains:
            problems.append(f"{b['booking_id']}: unknown train")
            continue
        jd = date.fromisoformat(b["journey_date"])
        if not runs(t, jd):
            problems.append(f"{b['booking_id']}: train {t} does not run on {jd}")
        codes = [s["station_code"] for s in stops[t]]
        if b["from_code"] not in codes or b["to_code"] not in codes or codes.index(b["from_code"]) >= codes.index(b["to_code"]):
            problems.append(f"{b['booking_id']}: {b['from_code']}->{b['to_code']} not on route")
        if b["class"] not in trains[t]["classes"].split(","):
            problems.append(f"{b['booking_id']}: class {b['class']} not on train")
        f = fares.get((t, b["class"], b["from_code"], b["to_code"]))
        n = len(trav[b["booking_id"]])
        if n == 0:
            problems.append(f"{b['booking_id']}: no travellers")
        elif f is None or abs(f * n - b["amount_paid"]) > 0.01:
            problems.append(f"{b['booking_id']}: amount_paid {b['amount_paid']} != fare {f} x {n}")
        if b["passenger_id"] not in passengers:
            problems.append(f"{b['booking_id']}: unknown passenger")
        exp = {"CONFIRMED": {"CNF", "WL"}, "RAC": {"RAC"}, "WAITLISTED": {"WL"}}.get(b["status"])
        if exp and not {x["status"] for x in trav[b["booking_id"]]} <= exp:
            problems.append(f"{b['booking_id']}: traveller statuses don't match {b['status']}")
        if b["status"] in ("RAC", "WAITLISTED") and not (0 <= (jd - start).days <= 13):
            problems.append(f"{b['booking_id']}: {b['status']} booking outside the next 14 days")
    check(not problems, "every booking points to a real train that runs that day, on its route, with the right fare",
          "; ".join(problems[:6]))
    check(len({b["booking_id"] for b in bookings}) == len(bookings), "booking ids are unique")

    # ---------- seats ----------
    cap = {r["name"].split("_")[1]: int(float(r["value"])) for r in settings if r["name"].startswith("capacity_")}
    booked = Counter()
    for b in bookings:
        if b["status"] in ("CONFIRMED", "RAC", "WAITLISTED"):
            for x in trav[b["booking_id"]]:
                if x["status"] == "CNF":
                    booked[(b["train_no"], b["journey_date"], b["class"])] += 1
    over = [k for k, n in booked.items() if n > cap.get(k[2], 0)
            or (k in seats and n + seats[k]["available"] > cap.get(k[2], 0))]
    check(not over, "seats booked never exceed seats available (booked + available <= capacity)", str(over[:5]))
    missing_days = []
    for t in trains:
        for day in range(14):
            d = start + timedelta(days=day)
            if runs(t, d):
                for c in trains[t]["classes"].split(","):
                    if (t, d.isoformat(), c) not in seats:
                        missing_days.append((t, d.isoformat(), c))
    check(not missing_days, "seat availability for the next 14 days for every running train/class", str(missing_days[:5]))
    check(all(s["available"] >= 0 and s["rac"] >= 0 and s["waitlist"] >= 0 for s in seats.values()), "seat counts non-negative")
    check(any(s["available"] == 0 and s["rac"] > 0 for s in seats.values()), "some trains full with RAC")
    check(any(s["available"] == 0 and s["rac"] == 0 and s["waitlist"] > 0 for s in seats.values()), "some trains waitlisted")
    for b in bookings:
        k = (b["train_no"], b["journey_date"], b["class"])
        if b["status"] == "RAC" and k in seats and not (seats[k]["available"] == 0 and seats[k]["rac"] > 0):
            failures.append(f"{b['booking_id']}: RAC booking but seats row is not in RAC state")
        if b["status"] == "WAITLISTED" and k in seats and seats[k]["available"] > 0:
            failures.append(f"{b['booking_id']}: waitlisted booking but berths are available")

    # ---------- awkward cases ----------
    sc = meta["scenarios"]
    check(not any(b["passenger_id"] == "P025" for b in bookings), "a passenger with no bookings (P025)")
    mixed = [b for b in bookings if {x["status"] for x in trav[b["booking_id"]]} >= {"CNF", "WL"}]
    check(bool(mixed), "a booking with one traveller confirmed and one waitlisted")
    midnight = [(t, s) for t, ss in stops.items() for s in ss[1:-1]
                if s["departure"] and s["departure"] < "03:31" and s["day_offset"] >= 1]
    check(bool(midnight), "a train that leaves a station after midnight")
    check(any(len(v) > 1 for v in sc["similar_station_names"].values()), "two stations with similar names (same city)")
    for key in ("refund_cutoff", "after_midnight", "mixed_cnf_wl", "train_cancelled_recently"):
        check(any(b["booking_id"] == sc[key]["booking_id"] for b in bookings), f"scenario booking {key} exists")
    rc = sc["refund_cutoff"]
    dep, at = datetime.fromisoformat(rc["departs"]), datetime.fromisoformat(rc["as_of_exactly_at_cutoff"])
    check(dep - at == timedelta(hours=48), "refund cut-off scenario is exactly 48 h before departure")

    # ---------- documents and settings ----------
    by_type = Counter()
    doc_ids = defaultdict(set)
    for d in docs:
        doc_ids[d["doc_type"]].add(d["doc_id"])
    for t, ids in doc_ids.items():
        by_type[t] = len(ids)
    check(by_type["rule"] >= 12, "at least 12 rules documents", str(dict(by_type)))
    check(by_type["circular"] >= 6, "at least 6 circulars", str(dict(by_type)))
    check(by_type["faq"] >= 20, "at least 20 FAQs", str(dict(by_type)))
    circ = {d["doc_id"]: d for d in docs if d["doc_type"] == "circular"}
    changing = [i for i, d in circ.items() if json.loads(d["supersedes"])]
    check(len(changing) >= 2, "at least 2 circulars change an earlier rule", str(changing))
    check(any(d["effective_from"] > start.isoformat() for d in circ.values()), "at least 1 circular takes effect in the future")
    faq_files = json.loads((ROOT / "data" / "docs" / "faqs.json").read_text(encoding="utf-8"))
    outdated = [f["doc_id"] for f in faq_files if "OUTDATED" in (f.get("note") or "")]
    check(len(outdated) >= 2, "at least 2 FAQs are out of date because of a circular", str(outdated))
    langs = {d["doc_id"]: d["lang"] for d in docs}
    check(sum(l in ("hi", "hinglish") for l in langs.values()) >= 3, "some documents in Hindi or Hinglish")

    text = {(d["doc_id"], d["section"]): d["text"] for d in docs}
    nums = lambda s: {n.replace(",", "").lstrip("0") or "0" for n in re.findall(r"\d[\d,]*", s)}  # noqa: E731
    mismatched = []
    for s in settings:
        doc, _, sec = (s["source_doc"] or "").partition("#")
        body = text.get((doc, sec))
        v = str(int(float(s["value"]))) if float(s["value"]).is_integer() else s["value"]
        if body is None or v not in nums(body):
            mismatched.append(f"{s['name']}={s['value']} not in {s['source_doc']}")
    check(not mismatched, "every settings value is stated in its source document section", "; ".join(mismatched[:5]))
    for name in ("cancel_tier1_hours", "cancel_flat_charge_2A", "tatkal_charge_SL", "action_ttl_minutes",
                 "boarding_change_cutoff_hours", "wl_clerkage_charge"):
        check(any(s["name"] == name for s in settings), f"setting {name} present")

    conn.close()
    print(f"Database: {db.db_path()}   (day 0 = {start})")
    print(f"Counts: {len(stations)} stations, {len(trains)} trains, {len(fares)} fares, {len(seats)} seat rows, "
          f"{len(passengers)} passengers, {len(bookings)} bookings {dict(statuses)}, "
          f"{by_type['rule']} rules, {by_type['circular']} circulars, {by_type['faq']} FAQs, {len(settings)} settings")
    for p in passes:
        print(f"  PASS  {p}")
    for f in failures:
        print(f"  FAIL  {f}")
    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
