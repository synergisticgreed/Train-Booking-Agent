# Ujjwal Rail Assistant

An AI travel assistant for **Ujjwal Rail**, a fictional train operator. It helps passengers find trains, check seats and fares, understand the rules, work out refunds, and book, cancel or change tickets after they confirm.

Built for the HCLTech *Future Ready AI Engineer* Hackathon.

> **Design principle:** the LLM understands the question and writes the reply. Plain Python code does everything exact: searching trains, checking seats, fares, refunds, deadlines and changing bookings.

---

## How to Run

### Prerequisites
- **Python 3.11+** (developed and tested on 3.12, Windows 11)
- A **Groq API key** (free at console.groq.com). Optional: without one, the app runs in offline mock mode.
- About 1 GB of disk space for the embedding model, which downloads automatically on first run

### 1. Install
```bash
pip install -r requirements.txt
```

### 2. Configure
Create a `.env` file in the project root by copying `.env.example`, and add your key:

```bash
# macOS / Linux / Git Bash
cp .env.example .env
```
```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

Then edit `.env`:
```ini
GROQ_API_KEY=gsk_your_key_here
LLM_MODEL=openai/gpt-oss-120b
MOCK_LLM=false
```

The app loads `.env` by itself, so nothing needs to be exported in the shell. **Never commit `.env`**; it is git-ignored.

### 3. Build the database
```bash
python data/generate_data.py
python data/validate_data.py
```

- `generate_data.py` builds `data/ujjwal.db` from the saved LLM output in `data/raw/`. **It makes no API calls** and takes a few seconds.
- `validate_data.py` runs 45 consistency checks. It should end with `45 passed, 0 failed`.
- Run `generate_data.py` again any time to **reset the demo data**, for example after test bookings or cancellations. It's safe while the API is running.

### 4. Start the API (terminal 1)
```bash
python -m uvicorn app.main:app --port 8000
```

When the log shows `ready: llm=groq:openai/gpt-oss-120b …`, check http://localhost:8000/health. It should return:
```json
{"status": "ok", "documents": 73, "llm": "groq:openai/gpt-oss-120b", "embedder": "st:…MiniLM-L12-v2", "passengers": 25}
```
Interactive API docs are at http://localhost:8000/docs.

### 5. Start the UI (terminal 2)
```bash
streamlit run ui/streamlit_app.py
```

Open http://localhost:8501.

- **Passenger ID** (sidebar): who is chatting, sent as the `X-Passenger-Id` header.
- **Simulated time** (sidebar, optional): an ISO time such as `2026-12-03T10:00:00+05:30`, sent as `as_of`, for demoing deadlines.

### 6. Try it
| Passenger | Message |
|---|---|
| P001 | `Which trains go from Pune to Nagpur on Friday evening?` |
| P001 | `Is the Tatkal charge for sleeper still Rs 150?` (the newer circular wins) |
| P001 | `Kya DigiLocker wala Aadhaar chalega?` (Hinglish, answered from a Hindi circular) |
| P001 | `Cancel booking UR-70001`, then press **Confirm** |
| P001 | `What's the status of booking UR-11952?` (refused, it's P002's) |
| P004 | `My train was cancelled 10 days ago and I still haven't got my money. I want to complain.` |
| P001 | `Trains from Colombo to Toronto` (not on the network) |

Demo passengers: **P001–P004** have scenario bookings, and **P025** has none. Details are in [data/DATA_NOTE.md](data/DATA_NOTE.md).

### Run the tests
```bash
pytest
```
72 tests. They run offline: mock LLM, offline embedder, temporary databases.

### Offline mode (no Groq key or no internet)
Set `MOCK_LLM=true` in `.env`, or leave `GROQ_API_KEY` empty. A rule-based parser and fixed reply templates replace the LLM; every other part works the same.

### Troubleshooting
| Problem | Fix |
|---|---|
| `address already in use` / port 8000 busy | An old API process is still running. Close its terminal, or in PowerShell run `Get-NetTCPConnection -LocalPort 8000 \| % { Stop-Process -Id $_.OwningProcess -Force }` |
| `/health` shows `"embedder": "hash"` | The embedding model is still downloading in the background. The app works meanwhile and switches automatically when the download finishes |
| `/health` shows `"llm": "mock"` | `GROQ_API_KEY` is missing from `.env`, or `MOCK_LLM=true` |
| Model not found (Groq) | The app automatically tries `openai/gpt-oss-120b`, then `openai/gpt-oss-20b`, then `qwen/qwen3.8-27b`. To force one, set `LLM_MODEL` in `.env` |
| `/health` shows `"documents": 0` or `"passengers": 0` | Run `python data/generate_data.py`, then restart the API |
| The UI says it can't reach the API | Start the API first, or set `API_URL` if it isn't on `localhost:8000` |
| Want fresh LLM-generated data | `python data/generate_data.py --fresh` (calls Groq about 42 times and takes about 5 minutes) |

### Configuration (`.env`)
| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key. If empty, the app uses mock mode |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Groq model. Others are tried automatically if it isn't available |
| `LLM_TIMEOUT` | `20` | Seconds per LLM call. On timeout the app falls back to templates |
| `MOCK_LLM` | `false` | `true` = no LLM calls at all |
| `DB_PATH` | `data/ujjwal.db` | SQLite database file |
| `DOCS_DIR` | `data/docs` | Rules, circulars and FAQ JSON files loaded at startup |
| `EMBEDDER` | `st` | `st` = multilingual sentence-transformers; `hash` = offline embedder |
| `EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Embedding model |
| `RETRIEVAL_MIN_COS` | `0.5` | Minimum similarity for a section to count as relevant |
| `API_URL` | `http://localhost:8000` | Where the UI finds the API |

---

## Features

- **Train search, seats and fares** come from SQLite through deterministic tools. The assistant never invents them.
- **Rule answers cite their source** (document ID and section). Retrieval combines BM25 keyword search with FAISS embeddings.
- **Newer rules win.** A circular overrides the older rule from its effective date, and an official rule overrides an FAQ. Any clash is logged.
- **Refunds are calculated by code** from the `settings` table and tested at every cut-off.
- **Safe changes:** propose → passenger confirms → code executes → code re-checks. Confirming is idempotent.
- **Hand-over to a person** for complaints, disputes, safety issues and low-confidence answers. The assistant never promises an outcome.
- **Privacy:** the passenger is identified only by the `X-Passenger-Id` header. Bookings that belong to someone else get a refusal that doesn't reveal whether they exist, and phone and ID numbers are masked.
- **Prompt-injection resistant:** retrieved content and user messages are treated as data, never as instructions.
- **English, Hindi and Hinglish:** replies use the passenger's language.
- **Live ingest:** `POST /ingest` makes new rules usable immediately, without a restart.
- **Audit trace** for every reply, which you can replay with `GET /trace/{trace_id}`.

## Architecture

![Request flow](ujjwal_rail_assistant_standard_flowchart.png)

| Step | Component | Type |
|---|---|---|
| 1 | Receive `POST /chat` (FastAPI) | Code |
| 2 | Authenticate from the `X-Passenger-Id` header | Code |
| 3 | Parse intent, slots and language | **LLM** |
| 4 | If slots are missing, ask a follow-up (max 2 in a row) | Code |
| 5 | Route by request type: **Lookup** (`tools.*` on SQLite) · **Policy** (BM25 + FAISS) · **Change** (preview → confirm API) · **Escalate** (hand over or refuse) | Code |
| 6 | Draft the reply, then a critic checks every number against tool output | **LLM** + Code |
| 7 | Save the audit trace to the `traces` table and return JSON | Code |

The pipeline is a single agent with a router. There is no multi-agent setup, because nothing in the brief needs one. The full design is in [architecture.md](architecture.md), [design.md](design.md) and [spec.md](spec.md).

## Tech Stack

| Layer | Choice |
|---|---|
| API | FastAPI + Uvicorn (Python 3.12) |
| UI | Streamlit chat with Confirm and Cancel buttons |
| LLM | Groq API (`openai/gpt-oss-120b`), with `MOCK_LLM=true` for offline testing |
| Retrieval | `rank_bm25` + FAISS, embeddings from `paraphrase-multilingual-MiniLM-L12-v2` |
| Storage | SQLite: trains, bookings, settings, documents, actions, hand-overs and traces |
| Packaging | Docker Compose (*planned*) |

We use the hosted Groq API instead of a locally hosted model. It's fast on a laptop with no GPU and needs no model container, and mock mode keeps everything testable offline. The reasoning is in [design.md](design.md) (D16).

## Project Structure

```
.
├── app/
│   ├── main.py            # FastAPI app and endpoints
│   ├── pipeline.py        # intent → route → compose → critic → trace
│   ├── llm.py             # Groq client and mock LLM
│   ├── retrieval.py       # BM25 + FAISS hybrid search, precedence rules
│   ├── tools/             # search_trains, seat_availability, fare, refund_quote, booking_status
│   ├── actions.py         # propose / confirm / cancel / verify
│   ├── escalation.py      # hand-over summaries per desk
│   ├── privacy.py         # ownership checks, PII masking, injection filtering
│   ├── replies.py         # fixed reply templates
│   └── db.py              # SQLite access
├── ui/streamlit_app.py    # chat UI
├── data/
│   ├── docs/              # rules.json, circulars.json, faqs.json
│   ├── prompts/           # exact LLM prompts used to generate the data
│   ├── raw/               # raw LLM replies
│   ├── generate_data.py   # builds data/ujjwal.db
│   ├── validate_data.py   # 45 data checks
│   ├── DATA_NOTE.md       # where the data came from and what it covers
│   └── DOCUMENTS.md       # list of document IDs
├── scripts/seed_demo.py   # loads the tiny test dataset instead (optional)
├── tests/                 # 72 tests
├── spec.md · architecture.md · design.md
├── requirements.txt
└── .env.example
```

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/chat` | Send a message. Header: `X-Passenger-Id`. Body: `session_id`, `message`, optional `as_of` |
| `POST` | `/actions/{id}/confirm` | Confirm a proposed change. Only the owner can confirm, and confirming twice has no extra effect |
| `POST` | `/actions/{id}/cancel` | Drop a proposed change |
| `GET` | `/bookings/{id}` | Fetch one booking (owner only) |
| `POST` | `/ingest` | Add or update a rule, circular or FAQ (takes effect immediately) |
| `GET` | `/trace/{trace_id}` | Replay how an answer was produced |
| `GET` | `/handoffs/{id}` | A hand-over record as staff receive it (owner only) |
| `GET` | `/health` | Readiness check, plus which LLM and embedder are active |

```bash
curl -X POST localhost:8000/chat \
  -H "X-Passenger-Id: P001" -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message":"Trains from Pune to Nagpur on 27 Nov after 6 pm?"}'
```
On Windows PowerShell, use `curl.exe` and escape the inner quotes as `\"`.

**Answer types:** `answer` · `clarification` · `confirm_needed` · `action_done` · `handed_to_human` · `not_found` · `refusal`

### Change permission levels

| Level | Scope | Behaviour |
|---|---|---|
| 0 | Lookups (search, fares, status, refund quote) | Answer immediately |
| 1 | Book, cancel or change the passenger's own booking within the rules | Preview → confirm → execute → verify |
| 2 | Refund disputes, concessions that need proof | Hand over to a person; never promise approval |
| 3 | Refund more than the rule allows, jump the waitlist, change a fare, act on someone else's booking | Refuse |

## Data

All data is fictional and was generated with an LLM (`openai/gpt-oss-120b` on Groq) by `data/generate_data.py`. Code then enforces consistency. See [data/DATA_NOTE.md](data/DATA_NOTE.md); prompts are in `data/prompts/` and raw replies in `data/raw/`.

- **Documents:** 12 rules, 6 circulars (2 change an earlier rule, 1 takes effect in the future) and 20 FAQs (2 out of date on purpose), some in Hindi and Hinglish. The full list is in [data/DOCUMENTS.md](data/DOCUMENTS.md).
- **Records:** 15 stations, 25 trains (350 fares), 14 days of seat availability (582 rows), 25 passengers and 54 bookings covering every status.
- **Edge cases:** a cancellation exactly at a refund cut-off and one minute after it, trains that leave after midnight, a booking with some travellers confirmed and others waitlisted, a passenger with no bookings, and stations with similar names.
- **Schema:** uses the required table and column names (`stations`, `trains`, `train_stops`, `fares`, `seats`, `passengers`, `bookings`, `booking_travellers`, `actions`, `settings`).

## Evaluation

*Planned:* a 30+ case eval set (`eval/`) with a results report covering correctness, groundedness, number accuracy, change safety, hand-over judgement, privacy and safety, and latency.

| Metric | Result |
|---|---|
| Correctness | _TBD_ |
| Groundedness | _TBD_ |
| Number accuracy | _TBD_ |
| Unsafe changes (target 0) | _TBD_ |
| Hand-over precision / recall | _TBD_ |
| Privacy or injection failures (target 0) | _TBD_ |
| Latency p50 / p95 | _TBD_ |

## Team & AI Usage

| Member | Contribution |
|---|---|
| _TBD_ | _TBD_ |

**AI-assisted parts:** _TBD (list the files or parts written with AI coding tools)._

## License

Hackathon submission. All data is fictional.
