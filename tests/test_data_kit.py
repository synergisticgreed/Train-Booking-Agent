"""The generated data kit rebuilds offline from data/raw (no LLM call) and passes every check."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"


@pytest.mark.skipif(not (RAW / "04_bookings.json").exists(), reason="data kit not generated yet")
def test_data_kit_rebuilds_offline_and_validates(tmp_path):
    env = {**__import__("os").environ, "DB_PATH": str(tmp_path / "kit.db"), "PYTHONIOENCODING": "utf-8"}
    build = subprocess.run([sys.executable, "data/generate_data.py", "--offline"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
    assert build.returncode == 0, build.stdout + build.stderr
    check = subprocess.run([sys.executable, "data/validate_data.py"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "0 failed" in check.stdout
