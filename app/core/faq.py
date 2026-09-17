"""FAQ knowledge base retrieval.

A compact BM25 implementation with a Chinese-friendly tokenizer:

* ASCII runs are lower-cased word tokens.
* CJK text is indexed as both single characters and character bigrams, which
  gives good recall for Chinese without pulling in a segmentation dependency.

The index is cached and automatically rebuilt when the knowledge base changes,
so administrators can edit FAQ entries at runtime and see the effect on the very
next question.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FaqEntry

_CJK = r"\u4e00-\u9fff"
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_CJK_CHAR_RE = re.compile(f"[{_CJK}]")

_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese/English text into searchable tokens."""
    text = (text or "").lower()
    tokens: list[str] = _ASCII_TOKEN_RE.findall(text)

    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(_CJK_CHAR_RE.findall(run))
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    return tokens


@dataclass
class FaqHit:
    id: int
    question: str
    answer: str
    category: str
    score: float
    matched_terms: list[str] = field(default_factory=list)

    def as_source(self) -> dict:
        return {"title": self.question, "category": self.category, "score": round(self.score, 3)}


@dataclass
class _Doc:
    entry_id: int
    question: str
    answer: str
    category: str
    length: int
    term_freq: dict[str, int]


class FaqIndex:
    """In-memory BM25 index over FAQ entries."""

    def __init__(self, entries: list[FaqEntry]):
        self._docs: list[_Doc] = []
        self._df: dict[str, int] = {}
        self._avg_len = 1.0
        self._idf: dict[str, float] = {}
        self._raw: dict[int, FaqEntry] = {}
        self._build(entries)

    # -- construction ------------------------------------------------------ #
    def _build(self, entries: list[FaqEntry]) -> None:
        for entry in entries:
            # Weight the question and the explicit keywords higher than the body
            # text so a close question match outranks a passing mention.
            weighted = " ".join(
                [
                    (entry.question + " ") * 3,
                    (entry.keywords + " ") * 2,
                    entry.answer,
                ]
            )
            tokens = tokenize(weighted)
            term_freq: dict[str, int] = {}
            for token in tokens:
                term_freq[token] = term_freq.get(token, 0) + 1

            self._docs.append(
                _Doc(
                    entry_id=entry.id,
                    question=entry.question,
                    answer=entry.answer,
                    category=entry.category,
                    length=len(tokens) or 1,
                    term_freq=term_freq,
                )
            )
            self._raw[entry.id] = entry
            for token in term_freq:
                self._df[token] = self._df.get(token, 0) + 1

        doc_count = len(self._docs)
        self._avg_len = (sum(doc.length for doc in self._docs) / doc_count) if doc_count else 1.0

        for token, freq in self._df.items():
            # BM25 idf with the +1 smoothing variant (always positive).
            self._idf[token] = math.log(1 + (doc_count - freq + 0.5) / (freq + 0.5))

    # -- query ------------------------------------------------------------- #
    def search(self, query: str, *, top_k: int = 3, min_score: float = 0.0) -> list[FaqHit]:
        query_tokens = tokenize(query)
        if not query_tokens or not self._docs:
            return []

        unique_query_tokens = set(query_tokens)
        scored: list[FaqHit] = []

        for doc in self._docs:
            score = 0.0
            matched: list[str] = []
            for token in unique_query_tokens:
                freq = doc.term_freq.get(token)
                if not freq:
                    continue
                idf = self._idf.get(token, 0.0)
                denominator = freq + _K1 * (1 - _B + _B * doc.length / self._avg_len)
                score += idf * (freq * (_K1 + 1)) / denominator
                matched.append(token)

            if score <= 0:
                continue

            # Bonus for a near-verbatim question match, which BM25 alone can
            # under-reward for very short entries.
            if query.strip() and (query.strip() in doc.question or doc.question in query.strip()):
                score += 2.5

            if score >= min_score:
                scored.append(
                    FaqHit(
                        id=doc.entry_id,
                        question=doc.question,
                        answer=doc.answer,
                        category=doc.category,
                        score=round(score, 4),
                        matched_terms=sorted(set(matched))[:8],
                    )
                )

        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:top_k]


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cache: FaqIndex | None = None
_cache_fingerprint: tuple[int, str] | None = None


def _fingerprint(db: Session) -> tuple[int, str]:
    rows = db.execute(select(FaqEntry.id, FaqEntry.updated_at)).all()
    if not rows:
        return (0, "")
    newest = max((row[1] for row in rows if row[1] is not None), default=None)
    return (len(rows), newest.isoformat() if newest else "")


def get_index(db: Session) -> FaqIndex:
    """Return the cached index, rebuilding it when the knowledge base changed."""
    global _cache, _cache_fingerprint

    fingerprint = _fingerprint(db)
    with _lock:
        if _cache is None or _cache_fingerprint != fingerprint:
            entries = list(db.execute(select(FaqEntry).where(FaqEntry.enabled.is_(True))).scalars())
            _cache = FaqIndex(entries)
            _cache_fingerprint = fingerprint
        return _cache


def invalidate_faq_cache() -> None:
    """Drop the cached index (called after FAQ create/update/delete)."""
    global _cache, _cache_fingerprint
    with _lock:
        _cache = None
        _cache_fingerprint = None


def retrieve_faq(
    db: Session,
    query: str,
    *,
    top_k: int = 3,
    min_score: float = 0.0,
) -> list[FaqHit]:
    """Search the FAQ knowledge base for ``query``."""
    try:
        return get_index(db).search(query, top_k=top_k, min_score=min_score)
    except Exception:  # noqa: BLE001 - retrieval must never break a conversation
        return []
