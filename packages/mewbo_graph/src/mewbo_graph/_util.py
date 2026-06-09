"""Dependency-free shared primitives for the mewbo_graph library.

Stdlib-only, imports NOTHING from the library's own submodules, so any layer
(``entities`` below ``wiki``, or ``wiki`` itself) can import it down/sideways
without a cycle. It hosts the two utilities that were previously duplicated /
reached-up for:

- ``cosine`` — the provider-agnostic cosine-similarity used by both the wiki
  embedder's vector math and the entity resolution ladder.
- ``utc_now_iso`` — the single ISO-8601 ``...Z`` clock that stamps memory and
  entity-mention provenance, so every layer agrees on the format.

``wiki.embedder.Embedder.cosine`` and ``wiki.memory.utc_now_iso`` delegate
here, keeping their public names/behaviour stable for existing callers.
"""
from __future__ import annotations

import datetime as _dt
import math


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 if either vector is zero-length/mismatched."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string.

    The single clock used to stamp memory provenance, refresh timestamps, and
    entity-mention provenance so every layer agrees on the format.
    """
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["cosine", "utc_now_iso"]
