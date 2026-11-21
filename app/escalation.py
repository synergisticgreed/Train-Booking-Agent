"""Hand-over to a person (R7). Code decides the desk and writes the summary; the reply never
promises an outcome. Each hand-over is stored in the `handoffs` table for staff."""
import re
import uuid
from datetime import datetime, timedelta

from app import db, privacy, tools

# (desk, category, pattern) - first match wins. Safety first.
_RULES = [
    ("safety", "safety_or_harassment",
     r"\b(harass\w*|unsafe|assault\w*|molest\w*|threat\w*|stalk\w*|theft|stolen|robbed|fight|drunk|"
     r"medical|emergency|fire|smoke in|chhed\w*|dar lag)\b"),
    ("refunds", "refund_dispute",
     r"\b(dispute|wrong refund|less refund|refund (is|was) (wrong|less|short)|"
     r"(haven'?t|have not|not|never|nahi)\s+(got|received|been credited|credited|mila)|still (waiting|haven'?t|not)|"
     r"missed (my |the )?train|tdr)\b"),
    ("concessions", "concession_claim", r"\b(claim|apply|proof|certificate)\b[^.?]*\bconcession|concession\b[^.?]*\b(claim|proof|certificate)"),
    ("complaints", "complaint", r"\b(complain\w*|shikayat|bad service|dirty|rude|unhappy|not happy|worst)\b"),
    ("customer_care", "wants_human", r"\b(human|real person|agent|someone|customer care|staff|officer)\b"),
]
DESK_NAMES = {"safety": "Safety & Security desk", "refunds": "Refunds desk", "concessions": "Concessions desk",
              "complaints": "Complaints desk", "customer_care": "Customer Care desk"}


def classify(message: str) -> tuple[str, str]:
    low = message.lower()
    for desk, category, pat in _RULES:
        if re.search(pat, low):
            return desk, category
    return "customer_care", "other"


def related_booking(pid: str, slots: dict, desk: str, as_of: datetime) -> tuple[dict | None, str | None]:
    """The passenger's booking this is about (own bookings only). Returns (booking, note_for_staff)."""
    bid = (slots.get("booking_id") or "").upper()
    if bid:
        if not privacy.owns_booking(pid, bid):
            return None, f"Passenger mentioned {bid}, which is not on their account (not disclosed to them)."
        return tools.booking_status(pid, booking_id=bid)["bookings"][0], None
    rows = tools.find_bookings(pid)
    if desk == "refunds":  # most recent cancelled / operator-cancelled booking in the last 60 days
        since = (as_of.date() - timedelta(days=60)).isoformat()
        rows = [b for b in rows if b["status"] in ("TRAIN_CANCELLED", "CANCELLED") and b["journey_date"] >= since] or rows
    if not rows:
        return None, None
    # Closest journey to today.
    b = min(rows, key=lambda r: abs((datetime.fromisoformat(r["journey_date"]).date() - as_of.date()).days))
    return tools.booking_status(pid, booking_id=b["booking_id"])["bookings"][0], None


def summarise(pid: str, desk: str, category: str, reason: str, message: str, booking: dict | None,
              note: str | None) -> str:
    p = db.query("SELECT name, phone_last4, preferred_language FROM passengers WHERE passenger_id = ?", (pid,))
    who = (f"{p[0]['name']} ({pid}, phone {privacy.mask_phone(p[0]['phone_last4'])}, "
           f"prefers {p[0]['preferred_language']})" if p else pid)
    lines = [f"Desk: {DESK_NAMES[desk]}. Category: {category}. Trigger: {reason}.",
             f"Passenger: {who}.",
             f'Message: "{privacy.redact(message.strip())[:400]}"']
    if booking:
        lines.append(f"Booking: {booking['booking_id']}, train {booking['train_no']}, {booking['from']} to "
                     f"{booking['to']}, journey {booking['journey_date']}, {booking['class']}, "
                     f"status {booking['status']}.")
    else:
        lines.append("Booking: none identified.")
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def hand_over(pid: str, message: str, slots: dict, as_of: datetime, trace_id: str,
              reason: str = "passenger_request", desk_category: tuple[str, str] | None = None) -> dict:
    desk, category = desk_category or classify(message)
    booking, note = related_booking(pid, slots, desk, as_of)
    summary = summarise(pid, desk, category, reason, message, booking, note)
    handoff_id = "HO-" + uuid.uuid4().hex[:6].upper()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO handoffs (handoff_id, passenger_id, desk, category, reason, booking_id, summary,
                                     trace_id, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (handoff_id, pid, desk, category, reason, booking["booking_id"] if booking else None, summary,
             trace_id, as_of.isoformat()))
    return {"handoff_id": handoff_id, "desk": desk, "category": category, "reason": reason,
            "booking_id": booking["booking_id"] if booking else None, "summary": summary}


def reply(h: dict, lang: str) -> str:
    desk = DESK_NAMES[h["desk"]]
    if lang in ("hinglish", "hi"):
        text = (f"Maine aapki baat {desk} ko ek summary aur aapki booking details ke saath bhej di hai "
                f"(reference {h['handoff_id']}). Staff ise dekh kar aapse sampark karega. Nateeja staff hi "
                f"tay karenge, isliye main koi vaada nahi kar sakta.")
        if h["desk"] == "safety":
            text = "Agar aap abhi khatre mein hain, turant train staff (TTE ya guard) ko batayein. " + text
        return text
    text = (f"I've passed this to the Ujjwal Rail {desk} with a short summary and your booking details "
            f"(reference {h['handoff_id']}). A staff member will review it and get back to you. "
            f"The outcome is decided by staff, so I can't promise a particular result.")
    if h["desk"] == "safety":
        text = "If you are in danger right now, please tell the train staff (TTE or guard) immediately. " + text
    return text
