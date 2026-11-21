# Architecture: Ujjwal Rail Assistant

> Status: **Phase 4 complete: safe and measured (capability level 4).** This document describes the whole system as built.

## 1. System Overview

```
┌──────────────────┐  HTTP + X-Passenger-Id  ┌──────────────────── FastAPI  app/main.py ─────────────────────┐
│ Streamlit UI     │ ──────────────────────▶ │ authenticate(header)                                          │
│ ui/streamlit_app │ ◀────────────────────── │  /chat ─▶ pipeline.run()          /actions/{id}/confirm|cancel │
│ (Confirm/Cancel) │      ChatResponse       │  /ingest /bookings /trace /handoffs /health                    │
└──────────────────┘                         └───────────────────────────┬───────────────────────────────────┘
   curl / judges' scripts ──────────────────────────────────────────────▶│
                                                                          ▼
   pipeline.py ── privacy.py (gates, masking) ── llm.py (Groq | mock: parse, phrase only)
        │
        ├── tools/      deterministic lookups: stations, dates, trains, seats, fares, bookings, refunds
        ├── retrieval.py  BM25 + FAISS, effective dates, source precedence
        ├── actions.py    preview / propose / confirm / execute / verify
        ├── escalation.py desk, summary, hand-over record
        ├── replies.py    exact reply templates
        └── db.py ─────▶ SQLite (data/ujjwal.db): timetable, bookings, settings, documents, actions,
                                                  handoffs, traces
```

**The LLM is called at most twice per chat turn:** once to parse the message and once to phrase the reply. It never decides a route, a permission or a number, and it is never on the confirm path.

## 2. Chat Turn (`POST /chat`)

| # | Stage | Module | LLM? |
|---|---|---|---|
| 1 | Authenticate from `X-Passenger-Id` only. Mask phone and ID numbers in the message | `main.py`, `privacy.redact` | No |
| 2 | **Gate:** "yes / confirm" while a change is pending → `confirm_needed` reminder. Never executes | `pipeline.py` | No |
| 3 | **Gate:** slipped-in instruction in the message → `refusal`, flagged in the trace | `privacy.is_injection` | No |
| 4 | Parse intent, language and slot *phrases* to JSON, with any pending follow-up as context | `llm.parse_intent` | **Yes** |
| 5 | Continue the pending follow-up if this message just fills in slots. Merge with session slots | `pipeline.py` | No |
| 6 | **Gate:** a booking question about another person → `refusal` | `privacy.asks_about_someone_else` | No |
| 7 | Route by intent (below) | `pipeline.py` | No |
| 8 | Phrase the answer from tool output or documents | `llm.compose_tools` / `llm.compose` | **Yes** |
| 9 | Critic: numbers must come from tool output or sources, and citations from retrieved sections. On failure → exact template | `pipeline.critic*`, `replies.py` | No |
| 10 | Mask PII in the reply, build the `audit` block, save the masked trace | `pipeline._finish`, `db.save_trace` | No |

**Routing:**

```
out_of_scope ─▶ refusal                         never_allowed ─▶ refusal (level 3)
escalate     ─▶ escalation.hand_over ─▶ handed_to_human
policy       ─▶ retrieval.search ─▶ privacy.sanitize_hits ─▶ compose ─▶ critic
                    no hits ─▶ not_found      docs don't answer ─▶ handed_to_human (low_confidence)
lookups      ─▶ tools.resolve_slots ─▶ missing / unclear? ─▶ clarification (≤ 2, then not_found)
                 train_search | seat_fare | booking_status | refund_quote ─▶ compose_tools ─▶ critic_numbers
changes      ─▶ (same slot step) ─▶ actions.preview_* ─▶ not allowed ─▶ refusal
                 cancel_booking | change_boarding | book_ticket      allowed ─▶ actions.propose ─▶ confirm_needed
                                                                               (fixed-template preview)
```

## 3. Change Path (`POST /actions/{id}/confirm`, no LLM)

```
get_action ── not the caller's / doesn't exist ─▶ 404 (same reply)
   │ done ─▶ replay stored result (200)          not pending ─▶ 409
   │ past expires_at ─▶ mark expired, 409
   ▼
preview_* again ── allowed & the fields the passenger saw are unchanged? ── no ─▶ mark failed, 409
   ▼ yes
BEGIN IMMEDIATE
  UPDATE actions SET status='executing' WHERE id=? AND status='pending'   -- must change 1 row
  guarded UPDATE/INSERT on bookings, booking_travellers, seats            -- rowcount checked
  UPDATE actions SET status='done', result=?
COMMIT   (any guard failure ─▶ ROLLBACK, mark failed, 409)
   ▼
verify(): fresh booking_status read ─▶ action_done + {"tool": "verify_booking", "output": {verified, booking}}
```

**Action states:** `pending → executing → done`. A pending action can also become `cancelled` (via `/cancel`), `expired` or `failed`.

## 4. Modules

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI routes, header auth, Pydantic models, startup (schema, `data/docs` load, index build) |
| `app/pipeline.py` | One chat turn (§2), session state, follow-ups, routing, critics, confirm and cancel wrappers, trace and audit |
| `app/llm.py` | `parse_intent`, `compose` (rules), `compose_tools` (tool results). Groq JSON mode at temperature 0. Mock parser and composer |
| `app/privacy.py` | `owns_booking`, `asks_about_someone_else`, `redact` / `mask_phone`, `is_injection`, `sanitize_hits`, refusal texts |
| `app/escalation.py` | `classify` (desk and category), `related_booking` (own bookings only), `summarise`, `hand_over` (writes `handoffs`), `reply` |
| `app/actions.py` | `preview_cancel` / `preview_change_boarding` / `preview_book` (no writes), `propose`, `confirm`, `cancel`, `_exec_*`, `verify` |
| `app/tools/common.py` | `resolve_station`, `resolve_date`, `parse_time_window`, `resolve_class`, `departure_at`, `runs_on`, `resolve_slots` |
| `app/tools/trains.py` | `search_trains`, `seat_availability`, `fare` |
| `app/tools/bookings.py` | `find_bookings`, `booking_status`, `refund_quote` (all scoped to `passenger_id` in SQL) |
| `app/retrieval.py` | BM25 + FAISS index over `documents` sections, reciprocal rank fusion, effective dates, precedence, swapped in a single step on rebuild |
| `app/replies.py` | Exact templates: follow-up questions, lookups, refunds, previews, confirmations, refusal reasons |
| `app/db.py` | Schema, `connect`, `transaction` (`BEGIN IMMEDIATE`), settings by date, traces, documents |
| `ui/streamlit_app.py` | Chat UI: passenger ID, optional simulated time, badges, sources, Confirm and Cancel buttons, tool and trace details |

## 5. Data Model

```
stations(station_code PK, name, other_names, city)
trains(train_no PK, name, runs_on, classes)
train_stops(train_no, station_code, stop_order, arrival, departure, day_offset, PK(train_no, stop_order))
fares(train_no, class, from_code, to_code, fare)
seats(train_no, journey_date, class, available, rac, waitlist)
passengers(passenger_id PK, name, phone_last4, preferred_language)
bookings(booking_id PK, passenger_id, train_no, journey_date, from_code, to_code, class, amount_paid, status, booked_at)
booking_travellers(booking_id, traveller_name, age, status, coach, berth)
actions(action_id PK, passenger_id, booking_id, action_type, status, created_at, expires_at, result JSON,
        payload JSON)                                                       -- payload: app-added
settings(name, value, source_doc, effective_from, PK(name, effective_from)) -- effective_from: app-added
documents(doc_id, section, doc_type, title, text, lang, topic, effective_from, supersedes JSON, updated_at,
          PK(doc_id, section))                                              -- app-added
handoffs(handoff_id PK, passenger_id, desk, category, reason, booking_id, summary, trace_id, created_at,
         status)                                                            -- app-added
traces(trace_id PK, passenger_id, session_id, created_at, payload JSON)     -- app-added
```

- `journey_date` is the date the train leaves its **origin** station. A stop's real date is `journey_date + day_offset`.
- `bookings.status` is one of CONFIRMED, RAC, WAITLISTED, CANCELLED or TRAIN_CANCELLED. Traveller status is CNF, RAC or WL.

## 6. Audit Trace (R10)

`traces.payload` holds everything about a reply. Each part is present when it applies:

| Key | Contents |
|---|---|
| `message`, `as_of`, `session_before` / `session_after` | Input and conversation state |
| `intent` | Parser output (engine, language, slot phrases) and `routed_intent` |
| `slots` | Raw, resolved, problems, missing |
| `retrieval` | Query, hits with scores, `upcoming`, `precedence_log` |
| `injections_removed` / `safety` | Stripped document sentences, injection or other-person flags |
| `tools` | Every tool call's input and output |
| `critic` | Passed or failed, the reason, the engine, and the rejected draft |
| `action_proposed` / `action_confirmed` / `action_cancelled` / `action_error` | The change at each step |
| `handoff` | Desk, category, reason, summary, booking |
| `response`, `latency_ms` | Exactly what was returned |
| **`audit`** | A flat summary of the above: asked, intent, documents found or overridden, tools run, change proposed/confirmed/cancelled, handed to, safety flags, answer type, answer, sources |

The whole payload is PII-masked before it is saved. `GET /trace/{id}` returns it.

## 7. Configuration (`.env`)

| Variable | Default | Used by |
|---|---|---|
| `GROQ_API_KEY` | — | `llm.py` |
| `LLM_MODEL` / `LLM_TIMEOUT` | `openai/gpt-oss-120b` / `20` s (auto-falls back through `FALLBACK_MODELS` if Groq says the model does not exist) | `llm.py` |
| `MOCK_LLM` | `false` | `llm.py` |
| `DB_PATH` / `DOCS_DIR` | `data/ujjwal.db` / `data/docs` | `db.py`, `main.py` |
| `EMBEDDER` / `EMBED_MODEL` | `st` / `paraphrase-multilingual-MiniLM-L12-v2`. Used only if fully cached; otherwise the hash embedder serves at once and the model downloads in the background and is swapped in (`/health` shows which) | `retrieval.py` |
| `RETRIEVAL_MIN_COS` | `0.5` | `retrieval.py` |
| `API_URL` | `http://localhost:8000` | `ui/streamlit_app.py` |

## 8. Running and Deployment

```
python scripts/seed_demo.py             # demo data (repeatable)
uvicorn app.main:app --port 8000        # API; reads .env itself (MOCK_LLM=true for offline)
streamlit run ui/streamlit_app.py       # UI on :8501
pytest                                  # 63 tests, mock mode + hash embedder
```

Planned `docker-compose.yml`, **not built yet**:
- `api` runs FastAPI on :8000, mounts `./data`, and reads `.env`.
- `ui` runs Streamlit on :8501, with `API_URL=http://api:8000`.

The LLM is the hosted Groq API, so no model container is needed. SQLite is a file on the shared volume.

## 9. Tests

| File | Covers |
|---|---|
| `tests/test_phase1.py` | Ingest, citations, precedence, effective dates, live ingest, out-of-scope, trace, rules critic |
| `tests/test_phase2.py` | Refund cut-offs (exact, ±1 min, 12 h, 4 h, departed), mixed confirmed/waitlisted booking, settings by date, live circular, search (time window, after midnight), aliases, follow-ups, privacy |
| `tests/test_phase3.py` | Full cancel flow, idempotent confirm, owner only, cancel endpoint, expiry, refund drift between preview and confirm, "yes" in chat, booking (confirmed and waitlisted), boarding change and its cut-off |
| `tests/test_phase4.py` | Hand-over per desk, no promises, Hinglish hand-over, low confidence, never-allowed requests, other-person refusals, identical refusals, PII masking, injections in messages and FAQs, audit trace, trace owner check |

Test data lives in `tests/fixtures/seed.sql` (timetable, bookings, settings) and `tests/fixtures/docs.json` (a mini corpus including an FAQ with a slipped-in instruction).
