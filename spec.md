# Specification: Ujjwal Rail Assistant

> Status: **Phase 4 complete: safe and measured (capability level 4).** 63 automated tests pass in mock mode.
> Still to do: the eval set and report, and Docker packaging (see §10).

## 1. Purpose

A chat assistant for the fictional operator **Ujjwal Rail**. It finds trains, checks seats and fares, explains rules, quotes refunds, books, cancels or changes tickets after the passenger confirms, and hands complaints to a person. It answers only from its own data and documents, and it cites its sources.

## 2. Scope

| In scope | Out of scope |
|---|---|
| Ujjwal Rail trains, seats, fares, bookings and rules | Hotels, cabs, other operators, real railways |
| English, Hindi and Hinglish text | Voice, images |
| Pretend booking and payment in SQLite | Real payments or ticketing |

## 3. Requirements and How Each Is Met

| ID | Requirement | Implementation | Tests |
|---|---|---|---|
| R1 | Understand ordinary language | The LLM (or the mock regexes) extracts intent, language and slot *phrases*. Code turns them into exact values: station aliases ("Bombay", "CST" → CSMT), dates ("kal", "this Friday", "5 Dec"), time windows and classes. | `test_station_aliases`, `test_hinglish_refund` |
| R2 | Ask when unsure | Required slots per intent. Missing ones are asked for in one question, and unclear stations get "did you mean…". At most **2** follow-ups in a row, and session slots are never asked for again. | `test_vague_request_*`, `test_at_most_two_followups_in_a_row`, `test_ambiguous_station_asks` |
| R3 | Answer from the rules | Hybrid retrieval (BM25 + FAISS) over document sections. Every answer cites `doc_id` and `section`. A critic rejects citations or numbers that aren't in the sources. | `test_rules_answer_cites_source`, `test_critic_*` |
| R4 | Code does the exact work | Tools in `app/tools/` handle trains, seats, fares, booking status and refunds. The LLM only phrases their output, and `critic_numbers` checks every number. | `test_refund_cutoffs` (7 cases), `test_train_search_*` |
| R5 | Ask before acting | Preview → `pending` action → separate `POST /actions/{id}/confirm` → guarded execution → verify. "Yes" typed in chat never confirms. | `test_cancel_full_flow`, `test_confirm_twice_is_idempotent`, `test_yes_in_chat_does_not_confirm` |
| R6 | Newer beats older | An in-effect circular `supersedes` a rule or section, a rule beats an FAQ on the same topic, and future circulars are mentioned but not applied. Overrides are logged. | `test_circular_supersedes_rule_and_faq`, `test_future_circular_*` |
| R7 | Hand over to a person | Complaints, refund disputes, missed-train refunds, concession claims, safety reports and requests for a human go to `handed_to_human` with a desk, a code-written summary and booking details. Low confidence is also handed over. No promised outcome. | `test_*_handed_*`, `test_low_confidence_hands_over`, `test_answerable_question_is_not_handed_over` |
| R8 | Keep passengers' data private | Identity comes only from the header. Every booking query is scoped in SQL. "Not yours" and "doesn't exist" give the same reply. Requests about another person ("my wife's ticket") are refused. Phone and ID numbers are masked in replies, LLM input and traces. | `test_refusal_identical_for_existing_and_missing`, `test_someone_elses_*`, `test_phone_numbers_masked_*` |
| R9 | Don't trust the content | Instruction-like messages are refused before any LLM call. Instruction-like sentences are stripped from retrieved documents. No routing or permission decision depends on LLM text. | `test_injection_in_message_changes_nothing`, `test_injection_inside_faq_is_stripped` |
| R10 | Keep a record | Every reply, confirm and cancel saves a trace, which includes a flat `audit` block. `GET /trace/{id}` replays it. | `test_audit_trace_records_full_cancel_story`, `test_trace_is_replayable` |
| R11 | Pick up rule changes live | `/ingest` rebuilds the index straight away and can set `settings` values from the document's effective date. | `test_ingest_takes_effect_immediately`, `test_ingested_circular_changes_refund_live` |
| R12 | Test it yourself | 63 unit and integration tests. **The 30+ question eval set and its report are still to do.** | — |

## 4. API Contract

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/chat` | `X-Passenger-Id` | Body `{session_id, message, as_of?}`. Returns a ChatResponse (§5) |
| POST | `/actions/{id}/confirm` | owner only | Optional body `{as_of?}`. 200 `action_done` (idempotent); 404 if it's not yours or doesn't exist; 409 if expired, cancelled, failed, or the preview has changed |
| POST | `/actions/{id}/cancel` | owner only | Drops a pending change. Nothing else changes. 409 if already done |
| GET | `/bookings/{id}` | owner only | Same 404 whether the booking isn't yours or doesn't exist |
| POST | `/ingest` | — | Adds or replaces a rule, circular or FAQ (§7). Takes effect immediately |
| GET | `/trace/{trace_id}` | trace ID; header optional | If `X-Passenger-Id` is sent it must be the trace's owner, otherwise 404 |
| GET | `/handoffs/{id}` | owner only | The hand-over record as the staff desk receives it |
| GET | `/health` | — | `{"status": "ok", "documents": n}` |

The header is required on every endpoint marked owner or `X-Passenger-Id` (401 if missing). Once the `passengers` table has rows, unknown IDs also get 401.

## 5. Response Shape (`/chat`, confirm, cancel)

```json
{
  "trace_id": "16 hex",
  "answer_type": "answer|clarification|confirm_needed|action_done|handed_to_human|not_found|refusal",
  "answer": "text, PII-masked",
  "sources": [{"doc_id": "CIR-2026-07", "section": "Revised charges"}],
  "tools": [{"tool": "refund_quote", "input": {}, "output": {}}],
  "pending_action": {"action_id": "ACT-…", "type": "cancel_booking", "booking_id": "UR-30418", "expires_at": "ISO8601"},
  "handed_to_human": {"handoff_id": "HO-…", "desk": "refunds", "summary": "…", "booking_id": "UR-41000"}
}
```

`pending_action` and `handed_to_human` are `null` unless they apply.

| answer_type | When |
|---|---|
| `answer` | Answered from documents or tools |
| `clarification` | A follow-up question (at most 2 in a row) |
| `confirm_needed` | A change was proposed. Nothing has happened yet |
| `action_done` | A confirmed change was carried out and checked (confirm endpoint only) |
| `handed_to_human` | A summary was filed for a staff desk |
| `not_found` | Nothing covers it, no matching booking, or the follow-up limit was reached |
| `refusal` | Out of scope, someone else's data, not allowed, or a slipped-in instruction |

## 6. Intents, Slots and Change Levels

| Intent | Required slots | Result |
|---|---|---|
| `policy` | — | Retrieval answer, `not_found`, or a hand-over on low confidence |
| `train_search` | from, to, date (time window and class optional) | `search_trains` (+ seats and fare per train if a class is given) |
| `seat_fare` | train_no, class (date optional) | `seat_availability` + `fare` |
| `booking_status` | — (booking ID or date optional) | Own bookings only |
| `refund_quote` | — (booking chosen by ID, date or the only upcoming one) | `refund_quote` |
| `cancel_booking` | — (booking chosen as above) | Preview + pending action |
| `change_boarding` | new_boarding | Preview + pending action |
| `book_ticket` | train_no, from, to, date, class (passengers 1–6, default 1) | Preview + pending action |
| `escalate` | — | Hand-over |
| `never_allowed` | — | Refusal |
| `out_of_scope` | — | Refusal |

| Level | Scope | Behaviour |
|---|---|---|
| 0 | Lookups and refund quotes | Answer |
| 1 | Book, cancel, or change the boarding station of your own booking, within the rules | Preview → confirm → execute → verify |
| 2 | Refund disputes, refunds not received, missed-train refunds, concession claims, safety | Hand over to a desk; never promise approval |
| 3 | Jump the waitlist, change a fare, refund more than the rules give, act on someone else's booking | Refuse |

## 7. Ingest Format and Settings

```json
{"doc_id": "CIR-2026-11", "doc_type": "rule|circular|faq", "title": "…", "effective_from": "YYYY-MM-DD",
 "lang": "en|hi|hinglish", "topic": "cancellation", "supersedes": ["RUL-CAN-01", "RUL-CAN-01#3. Refund table"],
 "sections": [{"section": "1", "text": "…"}],
 "settings": [{"name": "cancel_flat_charge_2A", "value": 150, "section": "1"}]}
```

Sending an existing `doc_id` replaces all its sections. Settings take effect from `effective_from`, and each gets `source_doc = "<doc_id>#<section>"`. Files in `data/docs/*.json` are loaded on startup.

| Setting | Used by |
|---|---|
| `cancel_tier1_hours`, `cancel_tier2_hours`, `cancel_no_refund_hours` | refund tiers |
| `cancel_flat_charge_<CLASS>`, `cancel_tier2_pct`, `cancel_tier3_pct` | refund charges |
| `wl_clerkage_charge`, `train_cancelled_refund_pct` | waitlisted travellers, trains cancelled by the operator |
| `boarding_change_cutoff_hours` | boarding change |
| `action_ttl_minutes` (default 15) | how long a proposed change stays open |

## 8. Non-Functional Requirements

- **LLM:** the Groq hosted API (`GROQ_API_KEY` in `.env`, never committed). `MOCK_LLM=true` gives deterministic offline behaviour. Any Groq failure falls back to the mock for that call.
- **Time:** every time-based decision uses `as_of` (default: now, IST).
- **UI:** a Streamlit chat page with a passenger-ID field and Confirm and Cancel buttons.
- **Performance:** p50 and p95 latency will be reported by the eval. There's no hard target.

## 9. Data Requirements

These tables are required, using the names from the brief: `stations`, `trains`, `train_stops`, `fares`, `seats`, `passengers`, `bookings`, `booking_travellers`, `actions`, `settings`. The app also adds:
- the tables `documents`, `traces` and `handoffs`
- the columns `settings.effective_from` and `actions.payload`

**Data kit (done):** LLM-generated by `data/generate_data.py`, with 15 stations, 25 trains, 582 seat rows over 14 days, 25 passengers, 54 bookings, 12 rules, 6 circulars, 20 FAQs and 38 settings, plus all the edge cases. `data/validate_data.py` passes 45/45 checks. See `data/DATA_NOTE.md`.

## 10. Known Gaps and Open Items

- **Still to build:**
  - the 30+ case eval set and report
  - `docker-compose.yml`
- **Mock mode:** replies for tool lookups are English templates. Hindi and Hinglish replies to tool answers need Groq. Hand-over and refusal texts do have Hinglish versions.
- **Live services:** Groq and the multilingual embedder have been checked by hand against the generated data. Automated tests use the mock LLM and the hash embedder.
- **Session memory** is in-process and is lost on restart.
