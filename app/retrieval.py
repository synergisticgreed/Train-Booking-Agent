"""Hybrid retrieval (BM25 + FAISS) over the documents table, with source precedence.

Precedence (applied after ranking, at the request's as_of date):
  1. Documents whose effective_from is in the future are not used; matching future
     circulars are returned separately as "upcoming".
  2. An in-effect circular that lists "DOC" or "DOC#section" in `supersedes` removes
     those sections from the results.
  3. Official sources (rule, circular) beat FAQs: an FAQ is dropped when an official
     hit on the same topic is present.
Every override is written to the returned precedence_log (and so to the trace).
"""
import logging
import os
import re
import threading
import time
import zlib
from datetime import date

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from app import db

log = logging.getLogger("retrieval")

AUTHORITY = {"circular": 3, "rule": 2, "faq": 1}
RRF_K = 60
_TOKEN = re.compile(r"[\wऀ-ॿ]+")
_STOP = {
    "a", "an", "the", "is", "are", "am", "i", "my", "me", "to", "of", "in", "on", "for", "and",
    "or", "it", "can", "do", "does", "what", "which", "how", "be", "with", "at", "this", "that",
    "will", "still", "if", "you", "your", "please", "hai", "kya", "ka", "ki", "ke", "ko", "se",
    "mein", "main", "hain", "aur",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


# ---------- embedders ----------

class HashEmbedder:
    """Character-trigram hashing. No download, deterministic; used for tests, offline runs and as the
    fallback while the multilingual model is unavailable."""
    dim = 512
    name = "hash"

    @property
    def min_cos(self) -> float:  # trigram cosines run high, so the gate is stricter
        return float(os.getenv("RETRIEVAL_MIN_COS_HASH", "0.9"))

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            s = f" {t.lower()} "
            for j in range(len(s) - 2):
                out[i, zlib.crc32(s[j:j + 3].encode()) % self.dim] += 1.0
        faiss.normalize_L2(out)
        return out


class STEmbedder:
    """Multilingual sentence-transformers model (handles English, Hindi and Hinglish)."""

    def __init__(self, model_name: str, local_only: bool):
        from sentence_transformers import SentenceTransformer
        self.name = f"st:{model_name}"
        self.model = SentenceTransformer(model_name, device="cpu", local_files_only=local_only)

    @property
    def min_cos(self) -> float:
        return float(os.getenv("RETRIEVAL_MIN_COS", "0.5"))

    def encode(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True).astype("float32")


def _model_name() -> str:
    return os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")


def _make_embedder():
    """Never blocks startup on the network: use the multilingual model only if it is fully cached;
    otherwise start on the hash embedder and fetch the model in the background (see _upgrade_later)."""
    if os.getenv("EMBEDDER", "st").lower() == "hash":
        return HashEmbedder(), False
    try:
        return STEmbedder(_model_name(), local_only=True), False
    except Exception as e:  # not cached / partial download / no sentence-transformers
        log.warning("multilingual embedder not cached (%s); using hash embedder until it downloads",
                    type(e).__name__)
        return HashEmbedder(), True


# ---------- index ----------

class Index:
    def __init__(self, embedder):
        self.embedder = embedder
        self.lock = threading.Lock()          # guards the snapshot (docs, bm25, faiss, embedder)
        self.build_lock = threading.Lock()    # serialises rebuilds (ingest vs background upgrade)
        self.docs: list[dict] = []
        self.bm25 = None
        self.faiss = None

    def rebuild(self, embedder=None) -> int:
        """Rebuild from SQLite. With `embedder`, also switch to it. The index and the embedder that
        built it are swapped together, so queries are always encoded by the matching model."""
        with self.build_lock:
            emb = embedder or self.embedder
            docs = db.list_documents()
            bm25 = faiss_index = None
            if docs:
                corpus = [f"{d['title']} {d['section']} {d['text']}" for d in docs]
                bm25 = BM25Okapi([tokenize(c) or ["_"] for c in corpus])
                vecs = emb.encode(corpus)
                faiss_index = faiss.IndexFlatIP(vecs.shape[1])
                faiss_index.add(vecs)
            with self.lock:  # atomic swap: readers never see a half-built index
                self.docs, self.bm25, self.faiss, self.embedder = docs, bm25, faiss_index, emb
        log.info("retrieval index rebuilt: %d sections (embedder=%s)", len(docs), emb.name)
        return len(docs)

    def search(self, query: str, as_of: date, k: int = 5) -> dict:
        with self.lock:
            docs, bm25, fidx, emb = self.docs, self.bm25, self.faiss, self.embedder
        empty = {"hits": [], "upcoming": [], "precedence_log": []}
        if not docs or not query.strip():
            return empty

        n = len(docs)
        bm = np.asarray(bm25.get_scores(tokenize(query)))
        sims, ids = fidx.search(emb.encode([query]), n)
        cos = np.zeros(n, dtype="float32")
        cos[ids[0]] = sims[0]

        min_cos = emb.min_cos
        relevant = [i for i in range(n) if bm[i] > 0 or cos[i] >= min_cos]
        as_of_s = as_of.isoformat()
        active = [i for i in relevant if docs[i]["effective_from"] <= as_of_s]
        future = [i for i in relevant if docs[i]["effective_from"] > as_of_s]

        # Reciprocal rank fusion of the two rankings.
        def fused(cands: list[int]) -> list[tuple[int, float]]:
            score = {i: 0.0 for i in cands}
            for ranking in (sorted(cands, key=lambda i: -bm[i]), sorted(cands, key=lambda i: -cos[i])):
                for r, i in enumerate(ranking):
                    score[i] += 1.0 / (RRF_K + r + 1)
            return sorted(score.items(), key=lambda x: -x[1])

        ranked = fused(active)[: k * 2]
        hits, plog = self._apply_precedence(docs, active, ranked)

        upcoming = [
            _public(docs[i], s) for i, s in fused(future)[:2] if docs[i]["doc_type"] == "circular"
        ]
        return {"hits": hits[:k], "upcoming": upcoming, "precedence_log": plog}

    @staticmethod
    def _apply_precedence(docs, active, ranked):
        plog: list[dict] = []

        # Rule 2: in-effect circulars supersede listed docs / sections (newest circular wins).
        superseded: dict[str, dict] = {}
        circulars = sorted(
            (docs[i] for i in active if docs[i]["doc_type"] == "circular"),
            key=lambda d: d["effective_from"],
        )
        for c in circulars:
            for target in c["supersedes"]:
                superseded[target] = c

        kept = []
        for i, s in ranked:
            d = docs[i]
            winner = superseded.get(f"{d['doc_id']}#{d['section']}") or superseded.get(d["doc_id"])
            if winner and winner["doc_id"] != d["doc_id"]:
                plog.append(_clash(winner, d, "superseded_by_circular"))
                continue
            kept.append((i, s))

        # Rule 3: official sources beat FAQs on the same topic.
        official_by_topic: dict[str, dict] = {}
        for i, _ in kept:
            d = docs[i]
            if d["topic"] and AUTHORITY[d["doc_type"]] > AUTHORITY["faq"]:
                official_by_topic.setdefault(d["topic"], d)

        hits = []
        for i, s in kept:
            d = docs[i]
            winner = official_by_topic.get(d["topic"]) if d["doc_type"] == "faq" else None
            if winner:
                plog.append(_clash(winner, d, "official_over_faq"))
                continue
            hits.append(_public(d, s))

        for entry in plog:
            log.info("precedence: %s", entry)
        return hits, plog


def _public(d: dict, score: float) -> dict:
    return {
        "doc_id": d["doc_id"], "section": d["section"], "doc_type": d["doc_type"],
        "title": d["title"], "text": d["text"], "topic": d["topic"], "lang": d["lang"],
        "effective_from": d["effective_from"], "supersedes": d["supersedes"],
        "score": round(float(score), 5),
    }


def _clash(winner: dict, loser: dict, reason: str) -> dict:
    return {
        "winner": winner["doc_id"], "winner_effective_from": winner["effective_from"],
        "overridden": f"{loser['doc_id']}#{loser['section']}", "reason": reason,
    }


# ---------- module-level singleton ----------

_index: Index | None = None
_init_lock = threading.Lock()


def get_index() -> Index:
    global _index
    with _init_lock:
        if _index is None:
            embedder, needs_download = _make_embedder()
            _index = Index(embedder)
            if needs_download:
                threading.Thread(target=_upgrade_later, args=(_index,), daemon=True,
                                 name="embedder-download").start()
    return _index


def _upgrade_later(index: Index, attempts: int = 30) -> None:
    """Download the multilingual model with retries (resumes partial files), then swap it in."""
    for attempt in range(1, attempts + 1):
        try:
            emb = STEmbedder(_model_name(), local_only=False)
            index.rebuild(embedder=emb)
            log.info("multilingual embedder ready; index switched from hash")
            return
        except Exception as e:
            wait = min(30 * attempt, 300)
            log.warning("embedder download attempt %d/%d failed (%s); retrying in %ds",
                        attempt, attempts, type(e).__name__, wait)
            time.sleep(wait)
    log.error("multilingual embedder unavailable; staying on hash embedder (retrieval still works)")


def embedder_name() -> str:
    return get_index().embedder.name


def rebuild() -> int:
    return get_index().rebuild()


def search(query: str, as_of: date, k: int = 5) -> dict:
    return get_index().search(query, as_of, k)
