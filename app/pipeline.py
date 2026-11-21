"""One turn of the assistant:
safety gates -> parse -> merge with session -> resolve slots -> (clarify | route) -> tools / retrieval /
actions / escalation -> compose -> critic -> redact -> audit trace."""
import json
import re
import threading
import time
import uuid
from datetime import datetime

from app import actions, db, escalation, llm, privacy, replies, retrieval, tools

BOOKING_INTENTS = {"booking_status", "refund_quote", "cancel_booking", "change_boarding"}

MAX_FOLLOWUPS = 2

# Required (resolved) slots per lookup intent. Booking intents pick the booking themselves.
REQUIRED = {
    "train_search": ["from", "to", "date"],
    "seat_fare": ["train_no", "class"],
    "booking_status": [],
    "refund_quote": [],
    "cancel_booking": [],
    "change_boarding": ["new_boarding"],
    "book_ticket": ["train_no", "from", "to", "date", "class"],
}
NEEDS_STATIONS = {"train_search", "seat_fare", "book_ticket", "change_boarding"}
_YES = re.compile(r"(yes|yes please|yeah|yep|ok|okay|confirm|confirmed|go ahead|do it|haan|haan ji|ha|theek hai|"
                  r"kar do|proceed)[.,! ]*(confirm( it)?|go ahead|please|do it)?[.,! ]*")

MESSAGES = {
    "out_of_scope": {
        "en": "Sorry, I can only help with Ujjwal Rail train travel.",
        "hinglish": "Maaf kijiye, main sirf Ujjwal Rail ki train yatra mein madad kar sakta hoon.",
        "hi": "क्षमा करें, मैं केवल उज्ज्वल रेल यात्रा में ही मदद कर सकता हूँ।",
    },
    "not_found": {
        "en": "I couldn't find this in Ujjwal Rail's rules, circulars or FAQs.",
        "hinglish": "Yeh jaankari Ujjwal Rail ke niyamon, circulars ya FAQs mein nahi mili.",
        "hi": "यह जानकारी उज्ज्वल रेल के नियमों, परिपत्रों या FAQ में नहीं मिली।",
    },
    "not_yet": {
        "en": "I can't help with that request yet.",
        "hinglish": "Is request mein main abhi madad nahi kar sakta.",
        "hi": "इस अनुरोध में मैं अभी मदद नहीं कर सकता।",
    },
    "no_booking": {
        "en": "I couldn't find a matching upcoming booking on your account.",
        "hinglish": "Aapke account mein aisi koi aane wali booking nahi mili.",
        "hi": "आपके खाते में ऐसी कोई आने वाली बुकिंग नहीं मिली।",
    },
}


def _msg(key: str, lang: str) -> str:
    return MESSAGES[key].get(lang, MESSAGES[key]["en"])


def _response(trace_id: str, answer_type: str, answer: str, sources=None, tools_=None,
              pending_action=None, handed_to_human=None) -> dict:
    return {
        "trace_id": trace_id,
        "answer_type": answer_type,
        "answer": answer,
        "sources": sources or [],
        "tools": tools_ or [],
        "pending_action": pending_action,
        "handed_to_human": handed_to_human,
    }


def _handoff_response(trace_id: str, pid: str, message: str, slots: dict, lang: str, as_of: datetime,
                      trace: dict, reason: str = "passenger_request") -> dict:
    h = escalation.hand_over(pid, message, slots, as_of, trace_id, reason=reason)
    trace["handoff"] = h
    public = {k: h[k] for k in ("handoff_id", "desk", "summary", "booking_id")}
    return _response(trace_id, "handed_to_human", escalation.reply(h, lang), handed_to_human=public)


def _public_action(pa: dict) -> dict:
    return {k: pa[k] for k in ("action_id", "type", "booking_id", "expires_at")}


# ---------- session state (in-process, keyed by passenger + session) ----------

_sessions: dict[tuple[str, str], dict] = {}
_sessions_lock = threading.Lock()


def _session(pid: str, sid: str) -> dict:
    with _sessions_lock:
        return _sessions.setdefault((pid, sid), {"pending": None, "slots": {}, "followups": 0, "action_id": None})


def reset_sessions() -> None:
    with _sessions_lock:
        _sessions.clear()


# ---------- main entry ----------

def run(passenger_id: str, session_id: str, message: str, as_of: datetime) -> dict:
    t0 = time.perf_counter()
    trace_id = uuid.uuid4().hex[:16]
    message = privacy.redact(message)  # phone / ID numbers never reach the LLM, the DB or a reply
    trace: dict = {"trace_id": trace_id, "passenger_id": passenger_id, "session_id": session_id,
                   "as_of": as_of.isoformat(), "message": message}
    state = _session(passenger_id, session_id)
    trace["session_before"] = dict(state)

    # "yes" in chat never confirms a change (R5): point to the confirm call instead.
    if state.get("action_id") and _YES.fullmatch(message.strip().lower()):
        resp = _remind_confirm(trace_id, passenger_id, state)
        if resp:
            return _finish(trace, resp, t0)

    # Slipped-in instructions are refused before any LLM sees the message (R9).
    if privacy.is_injection(message):
        trace["safety"] = {"injection_detected": True}
        _clear_pending(state)
        resp = _response(trace_id, "refusal", privacy.refusal("injection", llm.detect_language(message)))
        return _finish(trace, resp, t0)

    # Greetings / thanks / "what can you do?": answered directly, no LLM call, never refused.
    kind = replies.small_talk_kind(message)
    if kind:
        trace["intent"] = {"intent": "small_talk", "kind": kind, "engine": "rule", "routed_intent": "small_talk"}
        _clear_pending(state)
        resp = _response(trace_id, "answer", replies.small_talk(
            kind, replies.small_talk_language(message, llm.detect_language(message))))
        return _finish(trace, resp, t0)

    # "What details are needed?" mid-conversation: repeat what we're waiting for (not a rules search).
    if replies.asks_what_is_needed(message):
        trace["intent"] = {"intent": "what_is_needed", "engine": "rule", "routed_intent": "what_is_needed"}
        lang = llm.detect_language(message)
        if state["pending"] and state.get("last_question"):
            resp = _response(trace_id, "clarification", state["last_question"])
        else:
            resp = _response(trace_id, "answer", replies.what_is_needed(lang))
        return _finish(trace, resp, t0)

    context = {"pending_intent": state["pending"], "known_slots": state["slots"]} if state["pending"] else None
    if state.get("last_trains"):
        context = {**(context or {}), "trains_just_listed": [t["train_no"] for t in state["last_trains"]]}
    parsed = llm.parse_intent(message, context)
    intent, lang, new_slots = parsed["intent"], parsed.get("language", "en"), parsed.get("slots", {})

    # "book that train" / "the first one" / "the 18:55 one": resolve against the trains we just listed.
    wants_train = intent in ("book_ticket", "seat_fare") or state["pending"] in ("book_ticket", "seat_fare")
    if wants_train and not new_slots.get("train_no") and state.get("last_trains"):
        ref = _train_reference(message, state["last_trains"])
        if ref:
            new_slots = {**new_slots, "train_no": ref}
            trace["train_reference"] = ref

    # A reply to our own follow-up question ("from Pune", "kal", "UR-30418") continues the pending
    # intent: it carries slots and either has no intent of its own or is just a few words.
    if state["pending"] and new_slots and (intent == "policy" or len(message.split()) <= 4):
        intent = state["pending"]
        trace["continued_pending_intent"] = True
    trace["intent"] = {**parsed, "routed_intent": intent}

    if intent in BOOKING_INTENTS and privacy.asks_about_someone_else(message):
        # "my friend's ticket": refuse without looking anything up, so nothing can leak (R8).
        trace["safety"] = {"other_person_request": True}
        resp = _response(trace_id, "refusal", privacy.refusal("not_your_booking", lang))
        _clear_pending(state)
    elif intent == "out_of_scope":
        resp = _response(trace_id, "refusal", _msg("out_of_scope", lang))
        _clear_pending(state)
    elif intent == "never_allowed":
        resp = _response(trace_id, "refusal", privacy.refusal("never_allowed", lang))
        _clear_pending(state)
    elif intent == "escalate":
        resp = _handoff_response(trace_id, passenger_id, message, new_slots, lang, as_of, trace)
        _clear_pending(state)
    elif intent == "policy":
        resp = _answer_policy(trace_id, passenger_id, message, parsed, as_of, trace)
        _clear_pending(state)
    elif intent in REQUIRED:
        slots = {**state["slots"], **new_slots}  # never re-ask for something already given
        if intent == "book_ticket" and not slots.get("train_no") and state.get("last_trains"):
            # Trains were just listed but it's unclear which one: ask, don't search again.
            state["slots"] = slots
            resp = _clarify(trace_id, "book_ticket", [], [], lang, state,
                            text=replies.which_train(state["last_trains"], lang))
        else:
            if intent == "book_ticket" and not slots.get("train_no"):
                # "I need a ticket to Delhi": find trains first, then the passenger books one of them.
                intent = "train_search"
                trace["intent"]["routed_intent"] = intent
            resp = _lookup(trace_id, intent, slots, passenger_id, message, lang, as_of, state, trace)
    else:
        resp = _response(trace_id, "not_found", _msg("not_yet", lang))
        _clear_pending(state)

    trace["session_after"] = dict(state)
    return _finish(trace, resp, t0)


def _finish(trace: dict, resp: dict, t0: float) -> dict:
    """Mask PII in the reply and the record, add the audit summary, save the trace (R8, R10)."""
    resp["answer"] = privacy.redact(resp["answer"])
    trace["response"] = resp
    trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    trace["audit"] = _audit(trace, resp)
    # Mask the whole record, not just `message`: the parser's echoed query, slots etc. may repeat it.
    masked = json.loads(privacy.redact(json.dumps(trace, ensure_ascii=False, default=str)))
    db.save_trace(trace["trace_id"], trace["passenger_id"], trace["session_id"], masked)
    return resp


def _audit(trace: dict, resp: dict) -> dict:
    """One flat, human-readable record of the reply: what was asked, found, run, changed and answered."""
    retrieval_ = trace.get("retrieval") or {}
    return {
        "asked": trace.get("message") or trace.get("endpoint"),
        "intent": (trace.get("intent") or {}).get("routed_intent"),
        "documents_found": [f"{h['doc_id']}#{h['section']}" for h in retrieval_.get("hits", [])],
        "documents_overridden": retrieval_.get("precedence_log", []),
        "injections_removed": trace.get("injections_removed", []),
        "tools_run": [t["tool"] for t in trace.get("tools", [])] or [t["tool"] for t in resp.get("tools", [])],
        "change_proposed": trace.get("action_proposed"),
        "change_confirmed": trace.get("action_confirmed"),
        "change_cancelled": trace.get("action_cancelled"),
        "handed_to": (trace.get("handoff") or {}).get("desk"),
        "safety_flags": trace.get("safety"),
        "answer_type": resp["answer_type"],
        "answer": resp["answer"],
        "sources": resp["sources"],
    }


def _remind_confirm(trace_id: str, pid: str, state: dict) -> dict | None:
    try:
        a = actions.get_action(pid, state["action_id"])
    except actions.ActionError:
        return None
    if a["status"] != "pending":
        return None
    pa = {"action_id": a["action_id"], "type": a["action_type"], "booking_id": a["booking_id"],
          "expires_at": a["expires_at"]}
    return _response(trace_id, "confirm_needed", replies.use_confirm_button(pa), pending_action=pa)


def _clear_pending(state: dict) -> None:
    state["pending"], state["followups"] = None, 0


# ---------- lookups (tools) ----------

def _lookup(trace_id, intent, slots, pid, message, lang, as_of, state, trace) -> dict:
    resolved, problems = tools.resolve_slots(slots, as_of.date(), need_stations=intent in NEEDS_STATIONS)
    missing = [s for s in REQUIRED[intent] if s not in resolved and not any(p["slot"] == s for p in problems)]
    trace["slots"] = {"raw": slots, "resolved": resolved, "problems": problems, "missing": missing}
    # Remember only what resolved cleanly; bad phrases are asked again.
    state["slots"] = {k: v for k, v in slots.items() if not any(p["slot"] == k for p in problems)}

    # A place that is not on the network at all (Colombo, Toronto) is not a typo: say so plainly instead
    # of asking the passenger to "check" it or asking for a date. Refusal when no named place is ours.
    off_network = [p for p in problems if p["slot"] in ("from", "to", "new_boarding")
                   and "unknown" in p and not p.get("suggestions")]
    if off_network:
        _clear_pending(state)
        any_ours = any(s in resolved for s in ("from", "to", "new_boarding"))
        trace["off_network"] = [p["text"] for p in off_network]
        return _response(trace_id, "not_found" if any_ours else "refusal",
                         replies.not_served([p["text"] for p in off_network], tools.served_cities(), lang))

    if missing or problems:
        text = None
        if intent == "book_ticket" and missing == ["class"] and "train_no" in resolved:
            text = replies.which_class(resolved["train_no"], _train_classes(resolved["train_no"]), lang)
        return _clarify(trace_id, intent, missing, problems, lang, state, text=text)

    handler = {"train_search": _do_train_search, "seat_fare": _do_seat_fare,
               "booking_status": _do_booking_status, "refund_quote": _do_refund_quote,
               "cancel_booking": _do_cancel, "change_boarding": _do_change_boarding,
               "book_ticket": _do_book}[intent]
    result = handler(resolved, pid, lang, as_of)
    trace["tools"] = result["tool_runs"]
    if intent == "train_search":  # remember what we showed, so "book that train" can refer to it
        found = result["tool_runs"][0]["output"]["trains"] if result["tool_runs"] else []
        state["last_trains"] = [{"train_no": t["train_no"], "name": t["name"], "departs": t["departs"]}
                                for t in found]

    if result["answer_type"] == "clarification":
        return _clarify(trace_id, intent, [], [], lang, state, text=result["template"])
    _clear_pending(state)
    pa = result.get("pending_action")
    state["action_id"] = pa["action_id"] if pa else state.get("action_id")
    if pa:
        trace["action_proposed"] = pa

    answer = result["template"]
    if result["answer_type"] == "answer":  # previews always use the exact template (R5)
        draft = llm.compose_tools(message, lang, result["tool_runs"])
        if draft and draft.get("answer"):
            ok, reason = critic_numbers(draft["answer"], result["tool_runs"])
            trace["critic"] = {"passed": ok, "reason": reason, "engine": draft["engine"], "draft": draft["answer"]}
            if ok:
                answer = draft["answer"]
        else:
            trace["critic"] = {"passed": True, "reason": "template used", "engine": "template"}
    return _response(trace_id, result["answer_type"], answer, sources=result.get("sources"),
                     tools_=[{"tool": t["tool"], "input": t["input"], "output": t["output"]}
                             for t in result["tool_runs"]],
                     pending_action=_public_action(pa) if pa else None)


def _clarify(trace_id, intent, missing, problems, lang, state, text=None) -> dict:
    if state["followups"] >= MAX_FOLLOWUPS:
        _clear_pending(state)
        still_needed = missing + [p["slot"] for p in problems if p["slot"] not in missing]
        return _response(trace_id, "not_found", replies.gave_up(still_needed or ["booking"], lang))
    state["pending"] = intent
    state["followups"] += 1
    state["last_question"] = text or replies.clarify(missing, problems, lang)
    return _response(trace_id, "clarification", state["last_question"])


# No bare "one"/"two": "the second one" must not match "one" as first.
_ORDINALS = [(r"\b(first|1st|pehli|pehla|pahli|pahla|option 1|number 1)\b", 0),
             (r"\b(second|2nd|doosri|dusri|doosra|option 2|number 2)\b", 1),
             (r"\b(third|3rd|teesri|tisri|option 3|number 3)\b", 2), (r"\b(fourth|4th|chauthi)\b", 3),
             (r"\b(last|aakhri|akhri)\b", -1)]
_GENERIC_NAME_WORDS = {"express", "special", "superfast", "mail", "passenger", "intercity", "night", "day",
                       "junction", "fast", "local", "train", "the"}


def _train_reference(message: str, trains: list[dict]) -> str | None:
    """Which of the trains we just listed does the passenger mean? None if it's unclear."""
    low = message.lower()
    nos = [t["train_no"] for t in trains]
    for pat, idx in _ORDINALS:
        if re.search(pat, low) and (idx == -1 or idx < len(nos)):
            return nos[idx]
    for t in trains:  # departure time: "the 18:55 one", "6:55 pm wali"
        hh, mm = t["departs"][-5:].split(":")
        h12 = int(hh) % 12 or 12
        if re.search(rf"\b{hh}:{mm}\b|\b{h12}:{mm}\b|\b{int(hh)}:{mm}\b", low):
            return t["train_no"]
    named = {t["train_no"] for t in trains for w in re.findall(r"[a-z]+", t["name"].lower())
             if len(w) >= 4 and w not in _GENERIC_NAME_WORDS and re.search(rf"\b{w}\b", low)}
    if len(named) == 1:
        return named.pop()
    if len(nos) == 1 and re.search(r"\b(that|this|it|same|the train|woh|wahi|yeh|isi|usi)\b", low):
        return nos[0]
    return None


def _train_classes(train_no: str) -> list[str]:
    rows = db.query("SELECT classes FROM trains WHERE train_no = ?", (train_no,))
    return rows[0]["classes"].split(",") if rows else []


def _run(tool_runs: list, name: str, fn, **kwargs) -> dict:
    out = fn(**kwargs)
    tool_runs.append({"tool": name, "input": {k: (v.isoformat() if isinstance(v, datetime) else v)
                                              for k, v in kwargs.items()}, "output": out})
    return out


def _do_train_search(r, pid, lang, as_of) -> dict:
    runs: list = []
    out = _run(runs, "search_trains", tools.search_trains, from_code=r["from"], to_code=r["to"],
               travel_date=r["date"], time_window=r.get("time_window"))
    extras = {}
    if r.get("class"):  # class given: add seats + fare for the first few trains
        for t in out["trains"][:5]:
            s = _run(runs, "seat_availability", tools.seat_availability, train_no=t["train_no"],
                     travel_date=r["date"], cls=r["class"], from_code=r["from"])
            f = _run(runs, "fare", tools.fare, train_no=t["train_no"], cls=r["class"],
                     from_code=r["from"], to_code=r["to"])
            extras[t["train_no"]] = f"{replies.seats_line(s)}, {replies.fare_line(f)}"
    return {"answer_type": "answer", "tool_runs": runs, "template": replies.train_search(out, extras)}


def _do_seat_fare(r, pid, lang, as_of) -> dict:
    runs: list = []
    seats = None
    if r.get("date"):
        seats = _run(runs, "seat_availability", tools.seat_availability, train_no=r["train_no"],
                     travel_date=r["date"], cls=r["class"], from_code=r.get("from"))
    f = _run(runs, "fare", tools.fare, train_no=r["train_no"], cls=r["class"],
             from_code=r.get("from"), to_code=r.get("to"))
    return {"answer_type": "answer", "tool_runs": runs,
            "template": replies.seat_fare(seats, f, needs_date=not r.get("date"))}


def _do_booking_status(r, pid, lang, as_of) -> dict:
    runs: list = []
    out = _run(runs, "booking_status", tools.booking_status, passenger_id=pid,
               booking_id=r.get("booking_id"), travel_date=r.get("date"), as_of=as_of)
    if r.get("booking_id") and not out["bookings"]:
        # Same reply whether the booking belongs to someone else or does not exist (R8).
        return {"answer_type": "refusal", "tool_runs": runs, "template": privacy.refusal("not_your_booking", lang)}
    if not out["bookings"]:
        return {"answer_type": "not_found", "tool_runs": runs, "template": _msg("no_booking", lang)}
    return {"answer_type": "answer", "tool_runs": runs, "template": replies.booking_status(out)}


def _pick_booking(r, pid, lang, as_of) -> tuple[str | None, dict | None]:
    """Which of the caller's bookings is meant: explicit id, else by date, else the only upcoming one.
    Returns (booking_id, None) or (None, early_result)."""
    candidates = tools.find_bookings(pid, r.get("booking_id"), r.get("date"), as_of, active_only=False)
    if r.get("booking_id") and not candidates:
        # Same reply whether it belongs to someone else or does not exist (R8).
        return None, {"answer_type": "refusal", "tool_runs": [], "template": privacy.refusal("not_your_booking", lang)}
    if not r.get("booking_id"):
        candidates = [b for b in candidates if b["status"] in ("CONFIRMED", "RAC", "WAITLISTED")]
    if not candidates:
        return None, {"answer_type": "not_found", "tool_runs": [], "template": _msg("no_booking", lang)}
    if len(candidates) > 1:
        return None, {"answer_type": "clarification", "tool_runs": [],
                      "template": replies.which_booking(candidates, lang)}
    return candidates[0]["booking_id"], None


def _propose(action_type: str, booking_id: str | None, pv: dict, pid: str, as_of: datetime) -> dict:
    """Shared tail for the three change handlers: refuse, or store a pending action and preview it."""
    runs = pv["tool_runs"]
    if not pv["allowed"]:
        return {"answer_type": "refusal", "tool_runs": runs,
                "template": replies.change_refused(pv["reason"], pv.get("check"))}
    pa = actions.propose(pid, action_type, booking_id, pv["preview"], as_of)
    runs.append({"tool": "propose_action", "input": {"type": action_type, "booking_id": booking_id}, "output": pa})
    sources = [_source(s) for s in pv["preview"].get("rule_sources", []) if s]
    return {"answer_type": "confirm_needed", "tool_runs": runs, "pending_action": pa, "sources": sources,
            "template": replies.preview(action_type, pv["preview"], pa)}


def _do_cancel(r, pid, lang, as_of) -> dict:
    booking_id, early = _pick_booking(r, pid, lang, as_of)
    if early:
        return early
    return _propose("cancel_booking", booking_id, actions.preview_cancel(pid, booking_id, as_of), pid, as_of)


def _do_change_boarding(r, pid, lang, as_of) -> dict:
    booking_id, early = _pick_booking(r, pid, lang, as_of)
    if early:
        return early
    pv = actions.preview_change_boarding(pid, booking_id, r["new_boarding"], as_of)
    return _propose("change_boarding", booking_id, pv, pid, as_of)


def _do_book(r, pid, lang, as_of) -> dict:
    pv = actions.preview_book(pid, r["train_no"], r["date"], r["class"], r["from"], r["to"],
                              r.get("passengers", 1), as_of)
    return _propose("book_ticket", None, pv, pid, as_of)


def _do_refund_quote(r, pid, lang, as_of) -> dict:
    runs: list = []
    booking_id, early = _pick_booking(r, pid, lang, as_of)
    if early:
        return early
    q = _run(runs, "refund_quote", tools.refund_quote, passenger_id=pid, booking_id=booking_id, as_of=as_of)
    sources = [_source(s) for s in q.get("rule_sources", [])]
    return {"answer_type": "answer" if not q.get("error") else "not_found", "tool_runs": runs,
            "template": replies.refund(q), "sources": sources}


def _source(ref: str) -> dict:
    doc, _, section = ref.partition("#")
    return {"doc_id": doc, "section": section}


# ---------- rules answers (retrieval) ----------

def _answer_policy(trace_id: str, pid: str, message: str, parsed: dict, as_of: datetime, trace: dict) -> dict:
    lang = parsed.get("language", "en")
    query = message if parsed.get("query", message) == message else f"{message} {parsed['query']}"
    res = retrieval.search(query, as_of.date())
    trace["retrieval"] = {
        "query": query,
        "hits": [{k: h[k] for k in ("doc_id", "section", "doc_type", "effective_from", "score")}
                 for h in res["hits"]],
        "upcoming": [{"doc_id": u["doc_id"], "section": u["section"], "effective_from": u["effective_from"]}
                     for u in res["upcoming"]],
        "precedence_log": res["precedence_log"],
    }
    # Retrieved text is data: strip instruction-like sentences before the LLM or a reply sees it (R9).
    hits, removed_h = privacy.sanitize_hits(res["hits"])
    upcoming, removed_u = privacy.sanitize_hits(res["upcoming"])
    if removed_h or removed_u:
        trace["injections_removed"] = removed_h + removed_u
    if not hits:
        return _response(trace_id, "not_found", _msg("not_found", lang))

    # Top 3 sections, plus any other in-effect circular that matched: circulars are the newer word and
    # often add to a rule (e.g. DigiLocker accepted as ID) without being its closest text match.
    context = hits[:3] + [h for h in hits[3:] if h["doc_type"] == "circular"][:2]
    draft = llm.compose(message, lang, context, upcoming)
    ok, reason = critic(draft, context + upcoming)
    trace["critic"] = {"passed": ok, "reason": reason, "engine": draft.get("engine")}
    if not ok:
        draft = llm.mock_compose(message, lang, context, upcoming)

    if not draft.get("answerable", True) or not draft.get("answer"):
        # Relevant documents exist but don't answer it: low confidence -> a person, not a guess (R7).
        return _handoff_response(trace_id, pid, message, parsed.get("slots", {}), lang, as_of, trace,
                                 reason="low_confidence")

    sources = list({(c["doc_id"], c["section"]): {"doc_id": c["doc_id"], "section": c["section"]}
                    for c in draft["citations"]}.values())  # de-duplicated, order kept
    return _response(trace_id, "answer", draft["answer"], sources=sources)


# ---------- critics ----------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text):
        n = n.replace(",", "")
        n = n.rstrip("0").rstrip(".") if "." in n else n
        out.add(n.lstrip("0") or "0")
    return out


def critic(draft: dict, docs: list[dict]) -> tuple[bool, str]:
    """Rules answers: citations must be retrieved docs, numbers must appear in those docs."""
    if not draft.get("answerable", True):
        return True, "not answerable"
    allowed_cites = {(d["doc_id"], d["section"]) for d in docs}
    cites = {(c.get("doc_id"), c.get("section")) for c in draft.get("citations", [])}
    if not cites:
        return False, "no citations"
    if not cites <= allowed_cites:
        return False, f"unknown citation(s): {sorted(cites - allowed_cites)}"
    source_text = " ".join(f"{d['doc_id']} {d['section']} {d['effective_from']} {d['text']}" for d in docs)
    extra = _numbers(draft.get("answer", "")) - _numbers(source_text)
    if extra:
        return False, f"numbers not in sources: {sorted(extra)}"
    return True, "ok"


def critic_numbers(answer: str, tool_runs: list[dict]) -> tuple[bool, str]:
    """Tool answers: every number in the text must appear in the tool inputs/outputs.
    12-hour forms of HH:MM times are also allowed ("18:30" may be written "6:30 pm")."""
    blob = json.dumps([{"i": t["input"], "o": t["output"]} for t in tool_runs], default=str)
    allowed = _numbers(blob)
    for hh, _ in re.findall(r"\b(\d{2}):(\d{2})\b", blob):
        allowed.add(str(int(hh) % 12 or 12))
    extra = _numbers(answer) - allowed
    return (False, f"numbers not in tool output: {sorted(extra)}") if extra else (True, "ok")


# ---------- confirm / cancel endpoints (no LLM involved) ----------

def confirm_action(pid: str, action_id: str, as_of: datetime) -> tuple[int, dict]:
    """POST /actions/{id}/confirm. Returns (http_status, ChatResponse-shaped body)."""
    trace_id = uuid.uuid4().hex[:16]
    trace = {"trace_id": trace_id, "passenger_id": pid, "session_id": None, "as_of": as_of.isoformat(),
             "endpoint": f"confirm {action_id}"}
    t0 = time.perf_counter()
    try:
        out = actions.confirm(pid, action_id, as_of)
    except actions.ActionError as e:
        trace["action_error"] = {"code": e.code, "detail": e.detail}
        if e.http_status == 404:
            resp = _response(trace_id, "not_found", "No such pending change on your account.")
        else:
            resp = _response(trace_id, "refusal", _action_error_text(e))
        _finish(trace, resp, t0)
        return e.http_status, resp
    a, result = out["action"], out["result"]
    trace["action_confirmed"] = {"action_id": action_id, "type": a["action_type"], "already_done": out["already_done"],
                                 "result": result}
    text = replies.done(a["action_type"], result)
    if out["already_done"]:
        text = "This change was already confirmed earlier; nothing new was done. " + text
    resp = _response(trace_id, "action_done", text,
                     tools_=[{"tool": "verify_booking", "input": {"booking_id": result["booking_id"]},
                              "output": result.get("verification")}])
    _finish(trace, resp, t0)
    return 200, resp


def cancel_action(pid: str, action_id: str) -> tuple[int, dict]:
    trace_id = uuid.uuid4().hex[:16]
    trace = {"trace_id": trace_id, "passenger_id": pid, "session_id": None, "endpoint": f"cancel {action_id}"}
    t0 = time.perf_counter()
    try:
        out = actions.cancel(pid, action_id)
    except actions.ActionError as e:
        trace["action_error"] = {"code": e.code}
        status, resp = e.http_status, _response(
            trace_id, "not_found" if e.http_status == 404 else "refusal",
            "No such pending change on your account." if e.http_status == 404 else _action_error_text(e))
        _finish(trace, resp, t0)
        return status, resp
    trace["action_cancelled"] = {"action_id": action_id, "already": out["already"]}
    resp = _response(trace_id, "answer", replies.action_dropped(action_id))
    _finish(trace, resp, t0)
    return 200, resp


def _action_error_text(e: "actions.ActionError") -> str:
    return {
        "action_expired": "This proposed change has expired. Please ask again to get a fresh preview.",
        "action_cancelled": "This proposed change was cancelled, so it can't be confirmed.",
        "action_failed": "This proposed change could not be completed earlier. Please ask again.",
        "action_done": "This change has already been carried out, so it can't be dropped.",
        "action_in_progress": "This change is already being processed.",
        "preview_no_longer_valid": "Things have changed since the preview (for example the refund amount or seat "
                                   "status), so nothing was done. Please ask again to see the new preview.",
        "booking_changed": "The booking changed before this could be carried out, so nothing was done.",
    }.get(e.code, "This change can't be carried out.")
