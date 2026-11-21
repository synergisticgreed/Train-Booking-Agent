"""Privacy (R8) and content safety (R9).

Privacy rules
- Identity comes only from the X-Passenger-Id header (see main.authenticate).
- Booking queries are scoped in SQL (WHERE passenger_id = ?); owns_booking() is the single check
  used before anything about a named booking is shown or acted on.
- "Not yours" and "does not exist" get the same reply, so a refusal never confirms a booking exists.
- Phone / ID numbers are masked in every answer and in every saved trace.

Content safety
- Passenger messages and retrieved documents are data. Instruction-like text inside documents is
  removed before it reaches the LLM or a reply; in messages it is flagged and refused.
"""
import re

from app import db

REFUSALS = {
    "not_your_booking": {
        "en": "Sorry, I can only share or act on bookings made from your own account. "
              "If this is your booking, please sign in with the account that made it.",
        "hinglish": "Maaf kijiye, main sirf aapke apne account ki bookings ki jaankari de sakta hoon. "
                    "Agar yeh aapki booking hai, to usi account se login karein.",
        "hi": "क्षमा करें, मैं केवल आपके अपने खाते की बुकिंग की जानकारी दे सकता हूँ।",
    },
    "injection": {
        "en": "I can't change how I apply Ujjwal Rail's rules. I can still help with trains, fares, "
              "your bookings or refunds as the rules allow.",
        "hinglish": "Main Ujjwal Rail ke niyam badal nahi sakta. Train, kiraya, aapki booking ya refund "
                    "mein niyamon ke hisaab se madad kar sakta hoon.",
        "hi": "मैं उज्ज्वल रेल के नियम नहीं बदल सकता, पर नियमों के अनुसार मदद कर सकता हूँ।",
    },
    "never_allowed": {
        "en": "Sorry, that isn't something I or Ujjwal Rail staff can do: fares, waitlist order and refund "
              "amounts are fixed by the rules.",
        "hinglish": "Maaf kijiye, yeh sambhav nahi hai: kiraya, waitlist ka kram aur refund ki rakam niyamon "
                    "se tay hoti hai.",
        "hi": "क्षमा करें, यह संभव नहीं है: किराया, प्रतीक्षा सूची और रिफंड नियमों से तय होते हैं।",
    },
}


def refusal(key: str, lang: str) -> str:
    return REFUSALS[key].get(lang, REFUSALS[key]["en"])


# ---------- ownership ----------

def owns_booking(passenger_id: str, booking_id: str) -> bool:
    return bool(db.query("SELECT 1 FROM bookings WHERE booking_id = ? AND passenger_id = ?",
                         (booking_id.upper(), passenger_id)))


_RELATIONS = r"(friend|wife|husband|brother|sister|mother|father|mom|dad|mummy|papa|colleague|son|daughter|" \
             r"uncle|aunt|cousin|neighbou?r|boss|bhai|behen|dost)"
_OTHER_PERSON = [
    re.compile(rf"\b{_RELATIONS}'?s\b[^?.]*\b(booking|ticket|pnr|status|refund|seat|berth)", re.I),
    re.compile(rf"\b(booking|ticket|pnr|status|refund)\b[^?.]*\b(of|for)\s+(my\s+)?{_RELATIONS}\b", re.I),
    re.compile(rf"\b(it'?s|is|belongs to)\s+(my\s+)?{_RELATIONS}'?s?\b", re.I),
    re.compile(r"\b(someone else|another passenger|other passenger|somebody else)'?s?\b", re.I),
]


def asks_about_someone_else(message: str) -> bool:
    """Booking questions phrased about another person ("my friend's ticket", "it's my wife's")."""
    return any(p.search(message) for p in _OTHER_PERSON)


# ---------- masking ----------

_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{5})(\d{4})(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?(\d{4})(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}([A-Z])\b")


def mask_phone(last4: str | None) -> str:
    return f"******{last4}" if last4 else "not on file"


def redact(text: str | None) -> str | None:
    """Mask phone numbers, Aadhaar-style 12-digit IDs and PAN numbers anywhere in text."""
    if not text:
        return text
    text = _AADHAAR.sub(lambda m: f"XXXX-XXXX-{m[1]}", text)
    text = _PHONE.sub(lambda m: f"******{m[2]}", text)
    return _PAN.sub(lambda m: f"XXXXX0000{m[1]}", text)


# ---------- prompt injection ----------

_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\s+(all\s+|any\s+|your\s+|the\s+|previous\s+|prior\s+|above\s+)*"
    r"(rules|instructions|policy|policies|prompt|guidelines|restrictions)"
    r"|\b(system|assistant|developer)\s*(prompt|message)?\s*:"
    r"|\byou are now\b|\bact as\b|\bpretend (to be|you are)\b|\bjailbreak\b"
    r"|\b(give|issue|approve|grant)\s+(me\s+|every\w*\s+|all\s+)?(a\s+)?(full|100%|complete|extra)\s+refund"
    r"|\bniyam\s+(bhool|ignore)\w*",
    re.I)


def is_injection(text: str) -> bool:
    return bool(_INJECTION.search(text or ""))


def strip_injections(text: str) -> tuple[str, list[str]]:
    """Remove sentences containing instruction-like text from retrieved content. Returns (clean, removed)."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    removed = [s for s in sentences if is_injection(s)]
    clean = " ".join(s for s in sentences if not is_injection(s)).strip()
    return clean, removed


def sanitize_hits(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """Copy of hits with injected instructions removed; drops sections left empty."""
    out, log = [], []
    for h in hits:
        clean, removed = strip_injections(h["text"])
        if removed:
            log.append({"doc_id": h["doc_id"], "section": h["section"], "removed": removed})
        if clean:
            out.append({**h, "text": clean})
    return out, log
