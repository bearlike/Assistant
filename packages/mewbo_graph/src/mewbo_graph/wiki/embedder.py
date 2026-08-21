"""Embedder — thin wrapper around ``litellm.embedding``.

LiteLLM is the project's canonical LLM client (chat completions already
route through it), so embeddings ride the same proxy plumbing for free.
This module's job is to:

1. Read the configured embedding model from ``wiki.embedding.model``.
2. Normalise it with the proxy prefix (``openai/<model>``) so LiteLLM
   sends the request to our OpenAI-compatible LiteLLM proxy instead of
   trying to dispatch directly to a provider SDK.
3. Wrap ``litellm.embedding`` so its return value materialises into our
   typed ``Embedding`` records (with slug + node_id + dim).
4. Provide ``cosine`` and ``search`` static helpers.

Embedding is I/O-bound, not CPU-bound: a large indexing pass spends its
time waiting on the network, one blocking call after another, while the
process itself sits near idle. ``_EmbeddingPacer`` is what turns that into
a bounded, rate-limit-aware pool of concurrent requests instead of a
serial loop — see its docstring for the pacing/backoff contract.

KISS: no third-party LangChain abstraction layer, no batching wrappers —
LiteLLM already handles batching and provider routing; this module adds
only the concurrency/pacing layer LiteLLM does not provide.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, runtime_checkable

import litellm
from mewbo_core.common import get_logger
from mewbo_core.config import get_config, get_config_value

from mewbo_graph._util import cosine as _cosine

from .types import Embedding

logging = get_logger(name="api.wiki.embedder")

# Sliding-window pacing looks back this far when counting requests/tokens
# against a per-minute ceiling.
_WINDOW_SECONDS = 60.0
# Upper bound on exponential backoff between retries, before jitter.
_MAX_BACKOFF_SECONDS = 30.0
# Rough chars-per-token ratio used to estimate a batch's token cost for TPM
# pacing. An estimate, not a tokenizer call — good enough to stay under a
# budget, not exact accounting.
_CHARS_PER_TOKEN_ESTIMATE = 4


@runtime_checkable
class EmbedderProtocol(Protocol):
    """The duck-typed embedder surface retriever/ingestor depend on.

    ``Embedder`` (litellm-backed) and ``_NullEmbedder`` (BM25-fallback null
    object) both satisfy this; typing against it instead of ``Any`` catches
    wiring errors at definition.
    """

    def embed_nodes(
        self, items: list[tuple[str, str]], *, slug: str = ""
    ) -> list[Embedding]:
        """Embed ``(node_id, text)`` pairs into ``Embedding`` records."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into a vector."""
        ...


def make_embedder() -> Embedder:
    """Construct the wiki Embedder using the configured proxy + model."""
    return Embedder()


def project_embedding_model(store: Any, slug: str) -> str | None:
    """The embedding model *slug* is indexed and searched with, or ``None``.

    ``None`` means "inherit ``wiki.embedding.model``" — the answer for every
    project indexed before the override existed, and for every project whose
    operator never set one.

    Best-effort by construction: a store that cannot be read answers ``None``
    rather than raising. Falling back to the deployment default is what the
    caller would have done anyway, so a store hiccup degrades to today's
    behaviour instead of failing an index or a search.

    Cost class: ``O(one record)`` — a single slug-keyed settings read.
    """
    if not slug:
        return None
    try:
        settings = store.get_project_settings(slug)
    except Exception:  # noqa: BLE001 - see the best-effort contract above
        return None
    return getattr(settings, "embedding_model", None) if settings else None


def make_embedder_for(store: Any, slug: str) -> Embedder:
    """Build the Embedder bound to *slug*'s own embedding model.

    THE reason this exists rather than each caller reading config: the write
    side and the read side have to agree. Two embedding models rarely share a
    vector width, and ``vector_search`` scores cosine over whatever is stored —
    so a query embedded with a different model than the vectors it is scored
    against returns wrong neighbours rather than an error. Resolving both sides
    through one function is what makes that agreement structural instead of a
    convention every new retrieval site has to remember.
    """
    return Embedder(model=project_embedding_model(store, slug))


def make_embedder_for_or_none(store: Any, slug: str) -> Embedder | None:
    """Build *slug*'s Embedder, or ``None`` when the caller may fall back to BM25.

    The graceful twin of :func:`make_embedder_for`, for the write paths where a
    missing embedding backend must degrade retrieval rather than fail an index.
    It resolves the project's model and then goes through
    :func:`make_embedder_or_none` rather than constructing directly, so there
    stays exactly ONE graceful construction path however the model was chosen.
    """
    return make_embedder_or_none(project_embedding_model(store, slug))


def make_embedder_or_none(model: str | None = None) -> Embedder | None:
    """Build an Embedder, or None if it can't be constructed (BM25-only).

    The single construction path for callers that must degrade gracefully
    when no embedding backend is configured — used by insight ingestion so a
    missing proxy never fails a write.

    *model* is the project's own embedding model, or ``None`` to inherit the
    deployment default. It is a defaulted parameter rather than a second
    function because every caller degrades identically; splitting them would
    give the graceful path two implementations, and a test patching one of them
    would let the other build a live Embedder and reach the network.
    """
    try:
        return Embedder(model=model)
    except Exception:
        return None


class _EmbeddingPacer:
    """Bounds in-flight embedding requests and paces them against RPM/TPM.

    One atomic class owns all pacing state: the concurrency ceiling (which
    can shrink under sustained 429 pressure), and the two sliding one-minute
    windows (request count, estimated token count) that ``requests_per_minute``
    / ``tokens_per_minute`` are checked against. A thread calls ``acquire``
    before issuing a request and ``release`` once it completes; between the
    two, the request counts against both the concurrency ceiling and the
    per-minute budgets.

    Cost class: ``O(1)`` amortised per ``acquire`` — the sliding-window prune
    is bounded by how many requests fit in a one-minute window at the
    configured rate, never by total call volume across a job.
    """

    def __init__(
        self,
        *,
        concurrency: int,
        requests_per_minute: int | None,
        tokens_per_minute: int | None,
    ) -> None:
        self._concurrency_limit = max(1, concurrency)
        self._requests_per_minute = requests_per_minute
        self._tokens_per_minute = tokens_per_minute
        self._lock = threading.Condition()
        self._active = 0
        self._request_times: deque[float] = deque()
        self._token_events: deque[tuple[float, int]] = deque()

    def acquire(self, estimated_tokens: int) -> None:
        """Block until a concurrency slot and RPM/TPM budget are available."""
        with self._lock:
            while True:
                now = time.monotonic()
                self._prune(now)
                slot_ok = self._active < self._concurrency_limit
                rpm_ok = (
                    self._requests_per_minute is None
                    or len(self._request_times) < self._requests_per_minute
                )
                spent_tokens = sum(tokens for _, tokens in self._token_events)
                tpm_ok = (
                    self._tokens_per_minute is None
                    or spent_tokens + estimated_tokens <= self._tokens_per_minute
                )
                if slot_ok and rpm_ok and tpm_ok:
                    self._active += 1
                    self._request_times.append(now)
                    self._token_events.append((now, estimated_tokens))
                    return
                # Nothing to wait ON precisely (a released slot notifies us
                # immediately), so a short poll interval is enough to notice
                # a per-minute window rolling forward.
                self._lock.wait(timeout=0.5)

    def release(self) -> None:
        """Free the concurrency slot an ``acquire`` reserved."""
        with self._lock:
            self._active -= 1
            self._lock.notify_all()

    def throttle(self) -> None:
        """Halve the concurrency ceiling under sustained 429 pressure.

        Never drops below 1 — a rate-limited run must always make forward
        progress, just more slowly.
        """
        with self._lock:
            self._concurrency_limit = max(1, self._concurrency_limit // 2)

    def _prune(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
        while self._token_events and self._token_events[0][0] < cutoff:
            self._token_events.popleft()


class Embedder:
    """Thin facade: ``litellm.embedding`` + typed ``Embedding`` records."""

    # Project convention: chat models go through the LiteLLM proxy as
    # ``openai/<model>`` so LiteLLM uses its OpenAI-compatible client
    # against ``llm.api_base`` instead of routing to a provider SDK.
    # Same rule applies to embedding model names.
    _PROXY_PREFIX = "openai/"

    def __init__(
        self,
        *,
        model: str | None = None,
        batch_size: int | None = None,
        concurrency: int | None = None,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        max_retries: int | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """Construct the Embedder from config + kwargs.

        ``sleeper`` is the retry wait, injected as a collaborator so a caller
        observing the backoff observes only THIS object's waiting. Patching
        ``time.sleep`` on the stdlib module instead reaches every module and
        every thread in the process: the observation picks up unrelated
        waiting, and neutering it turns any concurrent poll loop into a spin.
        """
        cfg = get_config()
        raw_model = model or get_config_value(
            "wiki", "embedding", "model", default="openai/text-embedding-3-small"
        )
        self.model = self._normalise_model(raw_model)
        self.batch_size = batch_size or int(
            get_config_value("wiki", "embedding", "batch_size", default=64)
        )
        self._concurrency = concurrency or int(
            get_config_value("wiki", "embedding", "concurrency", default=4)
        )
        self._requests_per_minute = requests_per_minute or get_config_value(
            "wiki", "embedding", "requests_per_minute", default=None
        )
        self._tokens_per_minute = tokens_per_minute or get_config_value(
            "wiki", "embedding", "tokens_per_minute", default=None
        )
        self._max_retries = (
            max_retries
            if max_retries is not None
            else int(get_config_value("wiki", "embedding", "max_retries", default=5))
        )
        self._sleep = sleeper or time.sleep
        self._api_base = cfg.llm.api_base or None
        self._api_key = cfg.llm.api_key or "missing"
        self._pacer = _EmbeddingPacer(
            concurrency=self._concurrency,
            requests_per_minute=self._requests_per_minute,
            tokens_per_minute=self._tokens_per_minute,
        )

    @staticmethod
    def enabled() -> bool:
        """True when node embedding is switched on (``wiki.embedding.enabled``).

        The switch lives with the class that owns embedding rather than beside
        one of its callers: every path that embeds — the full index and the
        scoped refresh — has to read the same knob, and an operator who turns
        embedding off has no way to tell which caller re-implemented the read.
        Defaults to on, so an absent key never silently disables retrieval.
        """
        return bool(get_config_value("wiki", "embedding", "enabled", default=True))

    @classmethod
    def _normalise_model(cls, model: str) -> str:
        """Ensure the model name carries a provider prefix LiteLLM understands.

        Bare names like ``gemini-embedding-001`` route directly to a
        provider SDK and bypass our proxy. Prepending ``openai/`` forces
        the OpenAI-compatible path against ``api_base``.
        """
        return model if "/" in model else f"{cls._PROXY_PREFIX}{model}"

    def embed_nodes(
        self,
        items: list[tuple[str, str]],
        *,
        slug: str = "",
    ) -> list[Embedding]:
        """Embed ``(node_id, text)`` pairs and return ``Embedding`` records."""
        if not items:
            return []
        texts = [text for _, text in items]
        vectors = self._embed(texts)
        return [
            Embedding(
                slug=slug,
                node_id=nid,
                vector=list(vec),
                model=self.model,
                dim=len(vec),
            )
            for (nid, _), vec in zip(items, vectors)
        ]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string. Stays cheap: never spins up a pool."""
        vectors = self._embed([text])
        return vectors[0] if vectors else []

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Issue one or more embedding calls, batching to ``batch_size``.

        Cost class: ``O(len(texts) / batch_size)`` requests, issued through a
        bounded thread pool — never more than ``concurrency`` in flight, and
        paced against ``requests_per_minute`` / ``tokens_per_minute`` when
        configured. Order is a hard contract: every caller depends on
        ``_embed`` returning one vector per input text, in input order, so
        each batch is submitted with its position and results are reassembled
        by index rather than by completion order.

        A single batch (the common case for ``embed_query`` and any job whose
        node count fits under ``batch_size``) is issued directly, with no
        thread pool spun up.
        """
        if not texts:
            return []
        batches = [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]
        if len(batches) == 1:
            return self._call_batch(batches[0])

        results: list[list[list[float]] | None] = [None] * len(batches)
        workers = min(self._pacer_concurrency_hint(), len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._call_batch, batch): idx
                for idx, batch in enumerate(batches)
            }
            for future in futures:
                idx = futures[future]
                results[idx] = future.result()

        out: list[list[float]] = []
        for vectors in results:
            assert vectors is not None  # every future resolved or raised above
            out.extend(vectors)
        return out

    def _pacer_concurrency_hint(self) -> int:
        """Thread-pool sizing hint — the pacer enforces the real bound.

        Sized off the CONFIGURED ceiling, not the pacer's live (possibly
        throttled-down) one, so a run that gets throttled keeps its existing
        worker threads — each blocks in ``_EmbeddingPacer.acquire`` until the
        lower ceiling admits it — rather than needing new threads spun up.
        """
        return max(1, self._concurrency)

    def _call_batch(self, batch: list[str]) -> list[list[float]]:
        """Issue one batched embedding call, retrying through 429s.

        A 429 is retried up to ``max_retries`` times: the provider's
        ``Retry-After`` header wins when present, otherwise exponential
        backoff with jitter. Sustained rate-limiting also shrinks the
        pacer's concurrency ceiling, so a rate-limited run degrades to
        slower rather than to failed. Any other exception propagates
        immediately — a different credential or a slower pace cannot fix a
        transport failure or a bad request.
        """
        estimated_tokens = self._estimate_tokens(batch)
        attempt = 0
        while True:
            self._pacer.acquire(estimated_tokens)
            try:
                resp = litellm.embedding(
                    model=self.model,
                    input=batch,
                    api_base=self._api_base,
                    api_key=self._api_key,
                )
            except litellm.RateLimitError as exc:
                self._pacer.release()
                attempt += 1
                if attempt > self._max_retries:
                    raise
                self._pacer.throttle()
                delay = self._retry_delay(exc, attempt)
                logging.warning(
                    "Embedding request rate-limited, retrying "
                    f"(attempt {attempt}/{self._max_retries}, waiting {delay:.1f}s)"
                )
                self._sleep(delay)
                continue
            except Exception:
                self._pacer.release()
                raise
            else:
                self._pacer.release()
                return [self._vector_of(row) for row in resp.data]

    @staticmethod
    def _vector_of(row: Any) -> list[float]:
        # litellm returns either a dict ({'embedding': [...], 'index': N})
        # or an EmbeddingResponse pydantic object — handle both.
        vec = row["embedding"] if isinstance(row, dict) else row.embedding
        return list(vec)

    @staticmethod
    def _estimate_tokens(batch: list[str]) -> int:
        """Rough token estimate for TPM pacing — not exact accounting."""
        return sum(max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE) for text in batch)

    @staticmethod
    def _retry_delay(exc: litellm.RateLimitError, attempt: int) -> float:
        retry_after = Embedder._parse_retry_after(exc)
        if retry_after is not None:
            return retry_after
        backoff = min(2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
        return backoff + random.uniform(0, backoff * 0.5)

    @staticmethod
    def _parse_retry_after(exc: litellm.RateLimitError) -> float | None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        raw = headers.get("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    # ── Vector math (provider-agnostic, no embedder state needed) ──────

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Cosine similarity. Returns 0.0 if either vector is zero-length.

        Delegates to the shared, dependency-free ``mewbo_graph._util.cosine`` so
        the wiki vector math and the entity resolution ladder can never desync.
        """
        return _cosine(a, b)

    @staticmethod
    def search(
        qvec: list[float],
        vectors: list[list[float]],
        k: int = 10,
    ) -> list[tuple[int, float]]:
        """Return ``(index, cosine_score)`` for the top-k matches, sorted desc."""
        if not vectors:
            return []
        scored = [(i, Embedder.cosine(qvec, v)) for i, v in enumerate(vectors)]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]
