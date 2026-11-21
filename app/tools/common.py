"""Deterministic slot resolution: stations, dates, times, classes. No LLM involved."""
import difflib
import re
from datetime import date, datetime, timedelta, timezone

from app import db

IST = timezone(timedelta(hours=5, minutes=30))
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DAYNAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

CLASS_ALIASES = {
    "SL": ["sl", "sleeper"],
    "3A": ["3a", "3ac", "3 ac", "third ac", "ac 3 tier", "3 tier"],
    "2A": ["2a", "2ac", "2 ac", "second ac", "ac 2 tier", "2 tier"],
    "1A": ["1a", "1ac", "1 ac", "first ac"],
    "CC": ["cc", "chair car"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


# ---------- stations ----------

def resolve_station(text: str) -> dict:
    """Returns {"code", "name"} | {"ambiguous": [..]} | {"unknown": text, "suggestions": [..]}.
    Matches code, name, comma-separated other_names and city; exact match beats partial.
    For an unknown name, `suggestions` holds close spellings of real stations ("Nagpr" -> Nagpur);
    an empty list means the place is simply not on the Ujjwal Rail network (Colombo, Toronto)."""
    q = _norm(text)
    if not q:
        return {"unknown": text, "suggestions": []}
    rows = db.query("SELECT station_code, name, other_names, city FROM stations")
    exact, partial = [], []
    key_owner: dict[str, dict] = {}
    for r in rows:
        keys = [r["station_code"], r["name"], *(r["other_names"] or "").split(","), r["city"] or ""]
        keys = [_norm(k) for k in keys if k and k.strip()]
        for k in keys:
            key_owner.setdefault(k, r)
        if q in keys:
            exact.append(r)
        elif any(q in k.split() or (len(q) >= 4 and q in k) for k in keys):
            partial.append(r)
    for found in (exact, partial):
        if len(found) == 1:
            return {"code": found[0]["station_code"], "name": found[0]["name"]}
        if len(found) > 1:
            return {"ambiguous": [{"code": r["station_code"], "name": r["name"]} for r in found]}
    # Whole phrase unknown: try its words, so filler ("Pune please", "Nagpur station") can't hide a name.
    words = [w for w in q.split() if len(w) >= 3 and w not in _FILLER]
    if len(words) < len(q.split()):
        hits = {}
        for w in words:
            r = resolve_station(w)
            if "code" in r:
                hits[r["code"]] = r
        if len(hits) == 1:
            return next(iter(hits.values()))
    close = difflib.get_close_matches(q, list(key_owner), n=3, cutoff=0.75)
    seen, suggestions = set(), []
    for k in close:
        r = key_owner[k]
        if r["station_code"] not in seen:
            seen.add(r["station_code"])
            suggestions.append({"code": r["station_code"], "name": r["name"]})
    return {"unknown": text, "suggestions": suggestions}


_FILLER = {"please", "pls", "station", "railway", "junction", "jn", "city", "the", "from", "to", "se", "tak",
            "ji", "please.", "thanks", "thank", "you", "me", "main", "hai"}


def served_cities() -> list[str]:
    return [r["city"] for r in db.query("SELECT DISTINCT city FROM stations WHERE city IS NOT NULL ORDER BY city")]


# ---------- dates and times ----------

_HI_WORDS = {
    "जनवरी": "jan", "फरवरी": "feb", "फ़रवरी": "feb", "मार्च": "mar", "अप्रैल": "apr", "मई": "may", "जून": "jun",
    "जुलाई": "jul", "अगस्त": "aug", "सितंबर": "sep", "सितम्बर": "sep", "अक्टूबर": "oct", "अक्तूबर": "oct",
    "नवंबर": "nov", "नवम्बर": "nov", "दिसंबर": "dec", "दिसम्बर": "dec",
    "आज": "aaj", "कल": "kal", "परसों": "parso",
    "सोमवार": "monday", "मंगलवार": "tuesday", "बुधवार": "wednesday", "गुरुवार": "thursday",
    "शुक्रवार": "friday", "शनिवार": "saturday", "रविवार": "sunday",
}
_HI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _devanagari_to_latin(text: str) -> str:
    """"5 दिसंबर" -> "5 dec", "कल" -> "kal"; Devanagari digits -> ASCII."""
    text = text.translate(_HI_DIGITS)
    for hi, en in _HI_WORDS.items():
        text = text.replace(hi, f" {en} ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_date(text: str, today: date) -> date | None:
    """Phrase -> date. "kal" is read as tomorrow (travel context). A day/month without a year
    is the next such date on or after today."""
    t = _norm(_devanagari_to_latin(text or ""))
    if not t:
        return None
    if m := re.fullmatch(r"(\d{4}) (\d{2}) (\d{2})", t):  # ISO after _norm turns '-' into ' '
        return date(int(m[1]), int(m[2]), int(m[3]))
    if t in ("today", "tonight", "aaj", "aaj raat"):
        return today
    if t in ("tomorrow", "kal"):
        return today + timedelta(days=1)
    if t in ("day after tomorrow", "parso", "parson"):
        return today + timedelta(days=2)
    if m := re.fullmatch(r"(this |next )?(mon|tue|wed|thu|fri|sat|sun)[a-z]*", t):
        ahead = (_DAYNAMES[m[2]] - today.weekday()) % 7
        if m[1] == "next " and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    day = month = year = None
    if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*(?: (\d{4}))?", t):
        day, month, year = int(m[1]), _MONTHS.get(m[2]), m[3]
    elif m := re.fullmatch(r"([a-z]{3})[a-z]* (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", t):
        day, month, year = int(m[2]), _MONTHS.get(m[1]), m[3]
    elif m := re.fullmatch(r"(\d{1,2}) (\d{1,2})(?: (\d{2,4}))?", t):  # dd/mm[/yyyy]
        day, month, year = int(m[1]), int(m[2]), m[3]
    if not (day and month):
        return None
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def parse_time_window(text: str) -> dict | None:
    """"after 6 pm" -> {"after": "18:00"}; "evening" -> {"after": "17:00"}."""
    t = (text or "").lower()
    if m := re.search(r"\b(after|before|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t):
        h, mins = int(m[2]), int(m[3] or 0)
        if m[4] == "pm" and h < 12:
            h += 12
        if m[4] == "am" and h == 12:
            h = 0
        if h > 23 or mins > 59:
            return None
        return {"after" if m[1] == "after" else "before": f"{h:02d}:{mins:02d}"}
    for words, window in (
        (("morning", "subah"), {"after": "04:00", "before": "12:00"}),
        (("afternoon", "dopahar"), {"after": "12:00", "before": "17:00"}),
        (("evening", "shaam", "sham"), {"after": "17:00"}),
        (("night", "raat"), {"after": "20:00"}),
    ):
        if any(w in t for w in words):
            return window
    return None


def resolve_class(text: str) -> str | None:
    t = _norm(text)
    for code, aliases in CLASS_ALIASES.items():
        if t == code.lower() or t in aliases:
            return code
    return None


def departure_at(train_no: str, station_code: str, journey_date: str) -> datetime | None:
    """Actual departure datetime (IST) from a station, for a train that leaves its origin on journey_date."""
    rows = db.query("SELECT departure, day_offset FROM train_stops WHERE train_no = ? AND station_code = ?",
                    (train_no, station_code))
    if not rows or not rows[0]["departure"]:
        return None
    hh, mm = map(int, rows[0]["departure"].split(":"))
    d = date.fromisoformat(journey_date) + timedelta(days=rows[0]["day_offset"])
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def runs_on(runs_on_field: str, d: date) -> bool:
    days = [x.strip().title()[:3] for x in (runs_on_field or "").split(",")]
    return "Dai" in days or WEEKDAYS[d.weekday()] in days


# ---------- slot resolution ----------

STATION_SLOTS = ("from", "to")


def resolve_slots(raw: dict, today: date, need_stations: bool) -> tuple[dict, list[dict]]:
    """Turn raw slot phrases into codes/dates. Returns (resolved, problems)."""
    resolved, problems = {}, []
    if need_stations:
        for slot in (*STATION_SLOTS, "new_boarding"):
            if raw.get(slot):
                r = resolve_station(raw[slot])
                if "code" in r:
                    resolved[slot], resolved[f"{slot}_name"] = r["code"], r["name"]
                else:
                    problems.append({"slot": slot, "text": raw[slot], **r})
    if raw.get("date"):
        d = resolve_date(raw["date"], today)
        if d:
            resolved["date"] = d.isoformat()
        else:
            problems.append({"slot": "date", "text": raw["date"], "unknown": raw["date"]})
    if raw.get("time_window"):
        w = parse_time_window(raw["time_window"])
        if w:
            resolved["time_window"] = w
    if raw.get("class"):
        c = resolve_class(raw["class"])
        if c:
            resolved["class"] = c
        else:
            problems.append({"slot": "class", "text": raw["class"], "unknown": raw["class"]})
    if raw.get("train_no") and re.fullmatch(r"\d{5}", str(raw["train_no"]).strip()):
        resolved["train_no"] = str(raw["train_no"]).strip()
    if raw.get("booking_id"):
        resolved["booking_id"] = str(raw["booking_id"]).strip().upper()
    if raw.get("passengers"):
        try:
            n = int(str(raw["passengers"]).strip())
            if 1 <= n <= 6:
                resolved["passengers"] = n
            else:
                problems.append({"slot": "passengers", "text": str(raw["passengers"]), "unknown": raw["passengers"]})
        except ValueError:
            pass
    # "Friday evening" / "kal shaam" may arrive entirely in time_window: recover the date part.
    if "date" not in resolved and not any(p["slot"] == "date" for p in problems) and raw.get("time_window"):
        rest = re.sub(r"\b(after|before|by)\s+\d{1,2}(:\d{2})?\s*(am|pm)?|\b(morning|afternoon|evening|night|"
                      r"subah|dopahar|shaam|sham|raat|ko|ki|ke|in the|on)\b", " ", str(raw["time_window"]), flags=re.I)
        d = resolve_date(re.sub(r"\s+", " ", rest).strip(), today)
        if d:
            resolved["date"] = d.isoformat()
    return resolved, problems
