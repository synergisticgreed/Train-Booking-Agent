"""Booking status and refund quotes. Every query is scoped to the caller's passenger_id."""
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app import db
from app.tools.common import departure_at

ACTIVE = ("CONFIRMED", "RAC", "WAITLISTED")


class MissingSetting(Exception):
    pass


def _boarding_date(b: dict) -> str:
    dep = departure_at(b["train_no"], b["from_code"], b["journey_date"])
    return dep.date().isoformat() if dep else b["journey_date"]


def find_bookings(passenger_id: str, booking_id: str | None = None, travel_date: str | None = None,
                  as_of: datetime | None = None, active_only: bool = False) -> list[dict]:
    """The caller's own bookings only. Filters by id, by boarding date, or (default) upcoming."""
    sql, params = "SELECT * FROM bookings WHERE passenger_id = ?", [passenger_id]
    if booking_id:
        sql += " AND booking_id = ?"
        params.append(booking_id)
    if active_only:
        sql += f" AND status IN ({','.join('?' * len(ACTIVE))})"
        params += ACTIVE
    rows = db.query(sql + " ORDER BY journey_date, booking_id", tuple(params))
    if travel_date:
        rows = [b for b in rows if _boarding_date(b) == travel_date or b["journey_date"] == travel_date]
    elif not booking_id and as_of:
        cutoff = (as_of.date() - timedelta(days=1)).isoformat()
        rows = [b for b in rows if b["journey_date"] >= cutoff]
    return rows


def _travellers(booking_id: str) -> list[dict]:
    return db.query(
        "SELECT traveller_name, age, status, coach, berth FROM booking_travellers WHERE booking_id = ?",
        (booking_id,))


def booking_status(passenger_id: str, booking_id: str | None = None, travel_date: str | None = None,
                   as_of: datetime | None = None) -> dict:
    rows = find_bookings(passenger_id, booking_id, travel_date, as_of)[:5]
    bookings = []
    for b in rows:
        dep = departure_at(b["train_no"], b["from_code"], b["journey_date"])
        bookings.append({
            "booking_id": b["booking_id"], "train_no": b["train_no"], "journey_date": b["journey_date"],
            "departs": dep.strftime("%Y-%m-%d %H:%M") if dep else None,
            "from": b["from_code"], "to": b["to_code"], "class": b["class"], "status": b["status"],
            "travellers": _travellers(b["booking_id"]),
        })
    return {"count": len(bookings), "bookings": bookings}


# ---------- refunds ----------

def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def refund_quote(passenger_id: str, booking_id: str, as_of: datetime) -> dict:
    """Refund if the booking were cancelled at as_of. All numbers come from `settings`
    (the value in force on as_of's date). Tiers, per traveller:
      WL traveller                         -> charge = wl_clerkage_charge
      hours_before >  cancel_tier1_hours   -> charge = cancel_flat_charge_<CLASS>
      hours_before >  cancel_tier2_hours   -> charge = max(flat, cancel_tier2_pct % of fare share)
      hours_before >  cancel_no_refund_hours -> charge = max(flat, cancel_tier3_pct % of fare share)
      otherwise                            -> charge = fare share (no refund)
    "More than N hours" is strict: exactly N hours falls in the next (costlier) tier."""
    rows = find_bookings(passenger_id, booking_id)
    if not rows:
        return {"found": False}
    b = rows[0]
    out = {"found": True, "booking_id": b["booking_id"], "train_no": b["train_no"],
           "journey_date": b["journey_date"], "class": b["class"], "status": b["status"],
           "amount_paid": float(b["amount_paid"])}

    on = as_of.date().isoformat()
    sources: set[str] = set()

    def setting(name: str) -> Decimal:
        v = db.get_setting(name, on)
        if v is None:
            raise MissingSetting(name)
        if v[1]:
            sources.add(v[1])
        return Decimal(v[0])

    paid = Decimal(str(b["amount_paid"]))
    try:
        if b["status"] == "CANCELLED":
            return {**out, "cancellable": False, "reason": "already_cancelled"}
        if b["status"] == "TRAIN_CANCELLED":
            pct = setting("train_cancelled_refund_pct")
            refund = _money(paid * pct / 100)
            return {**out, "cancellable": False, "reason": "train_cancelled_by_operator",
                    "charge": float(paid - refund), "refund": float(refund), "rule_sources": sorted(sources)}

        dep = departure_at(b["train_no"], b["from_code"], b["journey_date"])
        delta = dep - as_of
        out["departs"] = dep.strftime("%Y-%m-%d %H:%M")
        out["hours_before_departure"] = round(delta.total_seconds() / 3600, 2)
        if delta <= timedelta(0):
            return {**out, "cancellable": False, "reason": "already_departed", "refund": 0.0}

        tier1 = timedelta(hours=float(setting("cancel_tier1_hours")))
        tier2 = timedelta(hours=float(setting("cancel_tier2_hours")))
        no_refund = timedelta(hours=float(setting("cancel_no_refund_hours")))

        travellers = _travellers(b["booking_id"]) or [{"traveller_name": "-", "status": "CNF"}]
        share = paid / len(travellers)
        lines, total = [], Decimal(0)
        for t in travellers:
            wl = t["status"] == "WL" or b["status"] == "WAITLISTED"
            if wl:
                tier, charge = "waitlisted", setting("wl_clerkage_charge")
            elif delta > tier1:
                tier, charge = f"over_{tier1.total_seconds() / 3600:g}h", setting(f"cancel_flat_charge_{b['class']}")
            elif delta > tier2:
                tier = f"{tier2.total_seconds() / 3600:g}_to_{tier1.total_seconds() / 3600:g}h"
                charge = max(setting(f"cancel_flat_charge_{b['class']}"), share * setting("cancel_tier2_pct") / 100)
            elif delta > no_refund:
                tier = f"{no_refund.total_seconds() / 3600:g}_to_{tier2.total_seconds() / 3600:g}h"
                charge = max(setting(f"cancel_flat_charge_{b['class']}"), share * setting("cancel_tier3_pct") / 100)
            else:
                tier, charge = f"within_{no_refund.total_seconds() / 3600:g}h", share
            charge = _money(min(charge, share))
            total += charge
            lines.append({"traveller": t["traveller_name"], "status": t["status"], "tier": tier,
                          "charge": float(charge)})
    except MissingSetting as e:
        return {**out, "error": f"missing_setting:{e}"}

    refund = _money(paid - total)
    return {**out, "cancellable": True, "charge": float(total), "refund": float(refund),
            "per_traveller": lines, "rule_sources": sorted(sources)}
