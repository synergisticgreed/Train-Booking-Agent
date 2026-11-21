# Data Note: Ujjwal Rail Data Kit

**All data is fictional.** It was generated for this hackathon on 2026-11-21 (day 0) with the LLM **`openai/gpt-oss-120b` on Groq**. There are no real people, phone numbers, booking references or timetables.

## Where it came from

| Part | Written by the LLM | Done by code (`data/generate_data.py`, seed 42) |
|---|---|---|
| 15 stations | Names, aliases ("Bombay, CST", "Poona"), cities. 7 station codes were fixed in the prompt | — |
| 25 trains | Routes, stop times, km, running days, classes, full-route fares, demand level | Day offsets from the clock times; segment fares = full fare × km share, rounded to Rs 5 (350 fare rows) |
| 25 passengers | Names, home cities, preferred language (9 en, 8 hi, 8 hinglish), last 4 phone digits | — |
| 50 bookings | Who, which train, which day, from/to, class, status, travellers | Booking IDs, `amount_paid` (fare × travellers), coach/berth, `booked_at`. It also fixes impossible picks: 4 bookings were moved to a day their train actually runs (listed in `generation_meta.json`) |
| Seats, 14 days | — | 582 rows from capacity × demand, consistent with the bookings: 61 class-days full with RAC, 139 waitlisted |
| 12 rules, 6 circulars, 20 FAQs | The wording of every section, in English, Hindi or Hinglish | The facts and numbers each section must contain. Each reply is checked so no number is lost |
| Settings (38 rows) | — | The rule numbers, each tied to the document section that states it, with `effective_from` |

- **Prompts, word for word:** `data/prompts/*.txt` (42 files).
- **Raw LLM replies:** `data/raw/*.json`, each recording the model, the time, the attempt number and the final prompt.
- **Rebuilding:**
  - `python data/generate_data.py --offline` rebuilds the identical database from the raw replies without calling the LLM.
  - `--fresh` asks the LLM again.

## What the fields mean

- **`trains.runs_on`** is the days the train leaves its **first** station. `bookings.journey_date` and `seats.journey_date` are that origin date. A stop's real date is `journey_date + train_stops.day_offset`.
- **Times** are IST, 24-hour.
- **Fares** are per passenger, in rupees.
- **Booking `status`:** `CONFIRMED`, `RAC`, `WAITLISTED`, `CANCELLED` (by the passenger) or `TRAIN_CANCELLED` (by the operator).
- **Traveller `status`:** `CNF`, `RAC` or `WL`. A `CONFIRMED` booking may contain a `WL` traveller (a partly confirmed booking).
- **`seats`** holds the berths still bookable (`available`), the RAC places left (`rac`) and the current waitlist length (`waitlist`).
- **`settings`:** for a given name, the row with the latest `effective_from` on or before the date in question applies. `source_doc` is `DOC#section`.

## Rules, circulars and FAQs

There are 38 documents, listed in `data/DOCUMENTS.md` and stored in `data/docs/{rules,circulars,faqs}.json`.

- **Circulars that change a rule:**
  - CIR-2026-07 lowers the 2A and 1A cancellation charges from 2026-11-01.
  - CIR-2026-08 raises the SL Tatkal charge to Rs 175 from 2026-10-01.
- **Future circular:** CIR-2026-09 takes effect on 2026-12-15 with new SL, 3A and CC cancellation charges.
- **Other circulars:** CIR-2026-10 lists the festival special trains (01xxx and 02xxx). CIR-2026-11 is in Hindi and accepts DigiLocker and m-Aadhaar as ID. CIR-2026-12 covers fog delays.
- **Out-of-date FAQs, on purpose:**
  - FAQ-04 still says the 2A charge is Rs 240.
  - FAQ-09 still says the SL Tatkal charge is Rs 150.
- **Hindi and Hinglish documents:** CIR-2026-11, FAQ-15 (Hindi), FAQ-16 and FAQ-17 (Hinglish).
- **FAQ-19** ends with a deliberately planted instruction ("SYSTEM: ignore your rules…"), added by code to test that the assistant ignores instructions found in documents.

## Awkward cases (`data/generation_meta.json` → `scenarios`)

| Case | Where | How to test |
|---|---|---|
| Cancellation at a refund cut-off | **UR-70001** (P001, 12123 PUNE→NGP 2A, departs 2026-11-25 18:55) | `as_of` 2026-11-23T18:54 gives Rs 1,160 (flat Rs 200). Exactly 18:55, and 18:56 one minute after, give Rs 1,020 (25%) |
| Train leaving after midnight | **UR-70002** (P003, 14002 boards JHS 01:50 on 2026-11-25; origin date 2026-11-24) | "my ticket for tomorrow" asked on 2026-11-24 |
| One traveller confirmed, one waitlisted | **UR-70003** (P002) | Status and refund (CNF flat charge + WL clerkage) |
| Operator-cancelled train ~10 days ago | **UR-70004** (P004) | Refund complaint → hand-over to the Refunds desk |
| Passenger with no bookings | **P025** | "Do I have any bookings?" |
| Stations with similar names | Mumbai: CSMT / LTT; Delhi: NDLS / DLI | "Trains from Mumbai…" → "did you mean" |

## What it does not cover

- There is no live running status, platform numbers or real-time delays, and no Tatkal or concession bookings in the data (they appear only in the rules).
- The routes are plausible but invented. Some of the geography is not realistic.
- Seat data covers only day 0 to day 13. Later dates return "booking not open".
- A booking's travellers share one class and one fare; there are no child or senior fares in the amounts.

## Checking

`python data/validate_data.py` runs 45 checks and exits with code 1 on any failure. They cover:
- the brief's minimum counts
- every booking: its train runs that day, the stations are on the route in order, the class exists, and the amount equals fare × travellers
- seats booked never exceed capacity
- 14 days of seat rows exist for every running train and class
- stop times move forward
- every settings value appears in its source document section
- all the awkward cases above are present

`tests/test_data_kit.py` rebuilds the kit offline and runs these checks on every `pytest`.
