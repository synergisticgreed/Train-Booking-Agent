"""Robustness: config fallbacks, model fallback, embedder fallback, Hindi dates, routing tweaks."""
from datetime import date

import pytest


def test_hindi_and_devanagari_dates():
    from app.tools.common import resolve_date
    today = date(2026, 11, 21)
    assert resolve_date("5 दिसंबर", today) == date(2026, 12, 5)
    assert resolve_date("१५ दिसम्बर", today) == date(2026, 12, 15)
    assert resolve_date("कल", today) == date(2026, 11, 22)
    assert resolve_date("परसों", today) == date(2026, 11, 23)


def test_no_key_means_mock(monkeypatch):
    from app import llm
    monkeypatch.setenv("MOCK_LLM", "false")
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert llm.use_mock() and llm.mode() == "mock"


def test_groq_model_fallback(monkeypatch):
    """If the configured model doesn't exist for the key, the next one is tried and remembered."""
    import httpx
    from groq import NotFoundError
    from app import llm

    calls = []

    class FakeCompletions:
        def create(self, model, **kw):
            calls.append(model)
            if model == "missing-model":
                req = httpx.Request("POST", "https://x")
                raise NotFoundError("model `missing-model` does not exist", response=httpx.Response(404, request=req),
                                    body=None)
            msg = type("M", (), {"content": '{"ok": true}'})
            return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    fake = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})})
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm, "_active_model", None)
    monkeypatch.setenv("LLM_MODEL", "missing-model")
    assert llm._groq_json("s", "u") == {"ok": True}
    assert calls[0] == "missing-model" and calls[1] == llm.FALLBACK_MODELS[0]
    assert llm._active_model == llm.FALLBACK_MODELS[0]


def test_embedder_falls_back_to_hash_when_model_missing(monkeypatch):
    from app import retrieval
    monkeypatch.setenv("EMBEDDER", "st")
    monkeypatch.setenv("EMBED_MODEL", "definitely/not-a-cached-model")
    emb, needs_download = retrieval._make_embedder()
    assert emb.name == "hash" and needs_download is True


def test_whole_numbers_for_llm():
    from app.llm import _whole_numbers
    assert _whole_numbers({"fare": 1600.0, "h": 56.5, "l": [2040.0]}) == {"fare": 1600, "h": 56.5, "l": [2040]}


def test_ticket_without_train_number_searches_first(seeded):
    r = seeded.post("/chat", headers={"X-Passenger-Id": "P001"}, json={
        "session_id": "t", "message": "Book a ticket from Pune to Nagpur on 5 Dec",
        "as_of": "2026-11-21T10:00:00+05:30"}).json()
    assert r["answer_type"] == "answer"
    assert r["tools"][0]["tool"] == "search_trains" and r["pending_action"] is None


def test_date_recovered_from_time_window():
    """Groq sometimes puts "Friday evening" entirely into time_window."""
    from app.tools.common import resolve_slots
    r, problems = resolve_slots({"time_window": "Friday evening"}, date(2026, 11, 21), need_stations=False)
    assert r["date"] == "2026-11-27" and r["time_window"] == {"after": "17:00"} and not problems
    r, _ = resolve_slots({"time_window": "kal shaam"}, date(2026, 11, 21), need_stations=False)
    assert r["date"] == "2026-11-22"


def test_places_off_the_network_are_not_treated_as_typos(seeded):
    def ask(msg, sid):
        return seeded.post("/chat", headers={"X-Passenger-Id": "P001"}, json={
            "session_id": sid, "message": msg, "as_of": "2026-11-21T10:00:00+05:30"}).json()
    r = ask("Trains from Colombo to Toronto tomorrow", "o1")
    assert r["answer_type"] == "refusal" and "doesn't have a station" in r["answer"] and "Pune" in r["answer"]
    r = ask("Trains from Pune to Toronto tomorrow", "o2")
    assert r["answer_type"] == "not_found" and "'toronto'" in r["answer"].lower()
    r = ask("Trains from Pune to Nagpr tomorrow", "o3")             # a typo of a real station
    assert r["answer_type"] == "clarification" and "Nagpur Junction (NGP)" in r["answer"]


def test_small_talk_is_answered_not_refused(seeded):
    def ask(msg):
        return seeded.post("/chat", headers={"X-Passenger-Id": "P001"}, json={
            "session_id": "st", "message": msg, "as_of": "2026-11-21T10:00:00+05:30"}).json()
    for msg in ("hello", "Hi!", "namaste ji", "Good morning", "what can you do?", "नमस्ते"):
        r = ask(msg)
        assert r["answer_type"] == "answer" and "Ujjwal Rail" in r["answer"], msg
    assert "welcome" in ask("thanks a lot")["answer"].lower()
    assert "journey" in ask("bye")["answer"].lower()
    # A greeting plus a real request still goes through the normal pipeline.
    assert ask("hi, book me a hotel in Delhi")["answer_type"] == "refusal"
