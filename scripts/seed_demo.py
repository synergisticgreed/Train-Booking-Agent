"""Load the TINY test dataset (tests/fixtures: 7 stations, 3 trains, 7 docs) into the app database.
For the real LLM-generated data kit use `python data/generate_data.py` instead (it rebuilds from
data/raw without calling the LLM if the kit was generated before).

    python scripts/seed_demo.py

Safe to run repeatedly and while the API is running. Restart the API afterwards so the search index
picks up the documents.
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402  (loads .env, so DB_PATH / DOCS_DIR are honoured)
import os  # noqa: E402

TABLES = ["stations", "trains", "train_stops", "fares", "seats", "passengers", "bookings",
          "booking_travellers", "actions", "settings", "documents", "handoffs", "traces"]


def main() -> None:
    db.init_db()
    seed_sql = (ROOT / "tests" / "fixtures" / "seed.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db.db_path(), timeout=30)
    try:
        conn.executescript("BEGIN;" + "".join(f"DELETE FROM {t};" for t in TABLES) + seed_sql + "COMMIT;")
    finally:
        conn.close()

    docs_dir = Path(os.getenv("DOCS_DIR", "data/docs"))
    if not docs_dir.is_absolute():
        docs_dir = ROOT / docs_dir
    docs_dir.mkdir(parents=True, exist_ok=True)
    for old in docs_dir.glob("*.json"):  # don't mix with the generated kit (same doc ids)
        old.unlink()
    shutil.copy(ROOT / "tests" / "fixtures" / "docs.json", docs_dir / "demo_docs.json")

    with sqlite3.connect(db.db_path()) as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("stations", "trains", "passengers", "bookings", "settings")}
    print(f"Seeded {db.db_path()}: {counts}")
    print(f"Demo documents copied to {docs_dir / 'demo_docs.json'} (loaded when the API starts)")
    print("Passengers: P001 (has UR-30418), P002 (has UR-48213, UR-41000), P003 (no bookings)")


if __name__ == "__main__":
    main()
