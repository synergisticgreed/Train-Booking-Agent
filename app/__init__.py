"""Ujjwal Rail Assistant.

Loads `.env` from the project root on import, so `uvicorn app.main:app` works without exporting
variables by hand. Variables already set in the shell win over `.env` (override=False).
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # python-dotenv missing: fall back to the real environment only
    pass

# Quieter, Windows-friendly Hugging Face downloads.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
