"""Embedder tests — ``litellm.embedding`` mocked at the client boundary."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import litellm
import pytest
from mewbo_graph.wiki.embedder import Embedder, make_embedder_for
from mewbo_graph.wiki.types import Embedding


def _embedding_response(vectors: list[list[float]]) -> MagicMock:
    """Return a MagicMock shaped like a ``litellm.EmbeddingResponse``.

    The real object has a ``.data`` list of ``{"embedding": [...], "index": N}``
    dicts. The Embedder only reads ``["embedding"]`` (or ``.embedding``), so a
    minimal stand-in is enough.
    """
    resp = MagicMock()
    resp.data = [{"embedding": vec, "index": i} for i, vec in enumerate(vectors)]
    return resp


@pytest.fixture(autouse=True)
def _patch_litellm_embedding():
    """Stop ``litellm.embedding`` from making real HTTP."""
    with patch("mewbo_graph.wiki.embedder.litellm.embedding") as mock_fn:
        yield mock_fn


def _build(model: str = "openai/test", batch_size: int = 8) -> Embedder:
    return Embedder(model=model, batch_size=batch_size)


class _SettingsStore:
    """One slug-keyed settings read, enough to witness model selection."""

    def __init__(self, model: str | None) -> None:
        self.model = model
        self.slugs: list[str] = []

    def get_project_settings(self, slug: str) -> SimpleNamespace:
        self.slugs.append(slug)
        return SimpleNamespace(embedding_model=self.model)


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("openai/text-embedding-3-large", "openai/text-embedding-3-large"),
        (None, "openai/text-embedding-3-small"),
    ],
)
def test_make_embedder_for_uses_project_override_or_default(
    monkeypatch, selected, expected
) -> None:
    """Both index writes and query reads resolve one identical effective model."""
    store = _SettingsStore(selected)
    monkeypatch.setattr(
        "mewbo_graph.wiki.embedder.get_config_value",
        lambda *keys, default=None: "text-embedding-3-small"
        if keys == ("wiki", "embedding", "model")
        else default,
    )

    embedder = make_embedder_for(store, "github.com/acme/beacon")

    assert embedder.model == expected
    assert store.slugs == ["github.com/acme/beacon"]


# ── embed_nodes ────────────────────────────────────────────────────────


def test_embed_nodes_returns_one_record_per_input(_patch_litellm_embedding):
    items = [("n1", "alpha"), ("n2", "beta"), ("n3", "gamma")]
    _patch_litellm_embedding.return_value = _embedding_response([
        [0.1, 0.1, 0.1, 0.1],
        [0.2, 0.2, 0.2, 0.2],
        [0.3, 0.3, 0.3, 0.3],
    ])
    emb = _build("openai/test")
    results = emb.embed_nodes(items, slug="x/y")
    assert len(results) == 3
    assert all(isinstance(r, Embedding) for r in results)
    assert [r.node_id for r in results] == ["n1", "n2", "n3"]
    assert [r.slug for r in results] == ["x/y"] * 3
    assert results[0].model == "openai/test"
    # Dim is taken from the response — no manual config required
    assert results[0].dim == 4
    _patch_litellm_embedding.assert_called_once()
    kwargs = _patch_litellm_embedding.call_args.kwargs
    assert kwargs["input"] == ["alpha", "beta", "gamma"]
    assert kwargs["model"] == "openai/test"


def test_embed_nodes_handles_variable_dim(_patch_litellm_embedding):
    """Whatever dim the provider returns flows through transparently."""
    _patch_litellm_embedding.return_value = _embedding_response([[0.0] * 3072])
    emb = _build()
    [record] = emb.embed_nodes([("only", "one")])
    assert record.dim == 3072


def test_embed_nodes_empty_short_circuits(_patch_litellm_embedding):
    emb = _build()
    assert emb.embed_nodes([]) == []
    _patch_litellm_embedding.assert_not_called()


def test_embed_nodes_batches_inputs(_patch_litellm_embedding):
    """Inputs exceeding ``batch_size`` are split across multiple calls."""
    _patch_litellm_embedding.side_effect = [
        _embedding_response([[1.0], [1.0]]),  # first batch of 2
        _embedding_response([[1.0]]),          # second batch of 1
    ]
    emb = _build(batch_size=2)
    out = emb.embed_nodes([("a", "1"), ("b", "2"), ("c", "3")])
    assert len(out) == 3
    assert _patch_litellm_embedding.call_count == 2
    assert _patch_litellm_embedding.call_args_list[0].kwargs["input"] == ["1", "2"]
    assert _patch_litellm_embedding.call_args_list[1].kwargs["input"] == ["3"]


def test_embed_query_returns_single_vector(_patch_litellm_embedding):
    _patch_litellm_embedding.return_value = _embedding_response([[0.7, 0.7]])
    emb = _build()
    out = emb.embed_query("hello")
    assert out == [0.7, 0.7]


# ── constructor wiring + model normalisation ──────────────────────────


def test_constructor_passes_proxy_base_url_and_key(_patch_litellm_embedding):
    """Embedder must point at our LiteLLM proxy, not direct provider routing."""
    _patch_litellm_embedding.return_value = _embedding_response([[0.0]])
    emb = _build("openai/my-model", batch_size=32)
    emb.embed_query("ping")
    kwargs = _patch_litellm_embedding.call_args.kwargs
    assert kwargs["model"] == "openai/my-model"
    # api_base + api_key are forwarded from LLMConfig
    assert "api_base" in kwargs
    assert "api_key" in kwargs


def test_constructor_prepends_proxy_prefix_for_bare_model_names():
    """Bare names like ``gemini-embedding-001`` get the ``openai/`` proxy prefix.

    Without this, LiteLLM dispatches the request to the provider SDK
    (e.g. Google Cloud) instead of routing through our OpenAI-compatible
    proxy at ``llm.api_base``.
    """
    emb = Embedder(model="gemini-embedding-001")
    assert emb.model == "openai/gemini-embedding-001"


def test_constructor_keeps_already_prefixed_model():
    emb = Embedder(model="openai/text-embedding-3-small")
    assert emb.model == "openai/text-embedding-3-small"
    # ``foo/bar`` is also passed through unchanged — any provider prefix counts
    emb2 = Embedder(model="azure/bar")
    assert emb2.model == "azure/bar"


# ── cosine + search ───────────────────────────────────────────────────


def test_cosine_orthogonal_is_zero():
    assert abs(Embedder.cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) - 0.0) < 1e-9


def test_cosine_identical_is_one():
    a = [0.5, 0.5, 0.5]
    assert abs(Embedder.cosine(a, a) - 1.0) < 1e-9


def test_cosine_handles_zero_vector():
    assert Embedder.cosine([0.0, 0.0, 0.0], [1.0, 1.0, 1.0]) == 0.0


def test_search_returns_top_k_indices_sorted_desc():
    qvec = [1.0, 0.0]
    vectors = [
        [0.0, 1.0],   # 0
        [1.0, 0.0],   # 1
        [0.7, 0.7],   # ~0.707
        [-1.0, 0.0],  # -1
    ]
    top = Embedder.search(qvec, vectors, k=2)
    assert top == [(1, pytest.approx(1.0)), (2, pytest.approx(0.7071, rel=1e-3))]


def test_search_k_larger_than_pool_returns_all():
    top = Embedder.search([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]], k=10)
    assert len(top) == 2


def test_search_empty_returns_empty():
    assert Embedder.search([1.0], [], k=5) == []


# ── concurrency: order, bound, backoff, no-deadlock ───────────────────


def test_embed_preserves_order_under_shuffled_completion(_patch_litellm_embedding):
    """The critical contract: order is INPUT order, not completion order.

    ``b`` finishes fastest, ``a`` slowest — a naive "collect as completed"
    implementation would return b, c, a. ``_embed`` must not.
    """
    delays = {"a": 0.06, "b": 0.0, "c": 0.02}

    def fake_embedding(*, model, input, api_base, api_key):
        text = input[0]
        time.sleep(delays[text])
        return _embedding_response([[float(ord(text))]])

    _patch_litellm_embedding.side_effect = fake_embedding
    emb = _build(batch_size=1)  # one text per batch -> pool is exercised
    out = emb.embed_nodes([("na", "a"), ("nb", "b"), ("nc", "c")])
    assert [r.node_id for r in out] == ["na", "nb", "nc"]
    assert [r.vector for r in out] == [[float(ord("a"))], [float(ord("b"))], [float(ord("c"))]]


def test_embed_respects_concurrency_bound(_patch_litellm_embedding):
    """Max observed in-flight embedding calls never exceeds ``concurrency``."""
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_embedding(*, model, input, api_base, api_key):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return _embedding_response([[0.0]])

    _patch_litellm_embedding.side_effect = fake_embedding
    emb = Embedder(model="openai/test", batch_size=1, concurrency=3)
    items = [(f"n{i}", f"t{i}") for i in range(10)]
    out = emb.embed_nodes(items)
    assert len(out) == 10
    assert max_active <= 3


def test_embed_retries_429_and_completes(_patch_litellm_embedding):
    """A 429 is retried (honouring backoff) and the call still succeeds."""
    rate_limit_error = litellm.RateLimitError(
        message="slow down", llm_provider="openai", model="openai/test"
    )
    _patch_litellm_embedding.side_effect = [
        rate_limit_error,
        rate_limit_error,
        _embedding_response([[1.0, 2.0]]),
    ]
    emb = Embedder(model="openai/test", batch_size=8, sleeper=lambda _: None)
    out = emb.embed_query("hello")
    assert out == [1.0, 2.0]
    assert _patch_litellm_embedding.call_count == 3


def test_embed_gives_up_after_max_retries(_patch_litellm_embedding):
    rate_limit_error = litellm.RateLimitError(
        message="slow down", llm_provider="openai", model="openai/test"
    )
    _patch_litellm_embedding.side_effect = rate_limit_error
    emb = Embedder(
        model="openai/test", batch_size=8, max_retries=2, sleeper=lambda _: None
    )
    with pytest.raises(litellm.RateLimitError):
        emb.embed_query("hello")
    # 1 initial attempt + 2 retries
    assert _patch_litellm_embedding.call_count == 3


def _rate_limit_error(retry_after: str | None) -> litellm.RateLimitError:
    """A 429 carrying (or omitting) a ``Retry-After`` header."""
    kwargs: dict[str, object] = {}
    if retry_after is not None:
        response = MagicMock()
        response.headers = {"retry-after": retry_after}
        kwargs["response"] = response
    return litellm.RateLimitError(
        message="slow down", llm_provider="openai", model="openai/test", **kwargs
    )


def test_embed_honours_retry_after_header(_patch_litellm_embedding):
    """``Retry-After`` wins over exponential backoff when present.

    The waits are collected through the injected ``sleeper`` — the ONLY thing
    that makes this list the embedder's own waiting. Reaching for
    ``time.sleep`` on the stdlib module records every sleep in the process,
    including other threads', and the assertion then fails on somebody else's
    poll loop.
    """
    slept: list[float] = []
    _patch_litellm_embedding.side_effect = [
        _rate_limit_error("7"),
        _embedding_response([[1.0]]),
    ]
    emb = Embedder(model="openai/test", batch_size=8, sleeper=slept.append)
    emb.embed_query("hello")
    assert slept == [7.0]


def test_embed_falls_back_to_backoff_without_retry_after(_patch_litellm_embedding):
    """The paired negative: no header ⇒ the first-attempt backoff band.

    Without this, ``== [7.0]`` above is satisfied by any implementation that
    happens to produce 7.0 — the header is only proven to WIN if the same seam
    produces something else when it is absent. Attempt 1's backoff is
    ``1 + uniform(0, 0.5)``, a band that cannot contain 7.0.
    """
    slept: list[float] = []
    _patch_litellm_embedding.side_effect = [
        _rate_limit_error(None),
        _embedding_response([[1.0]]),
    ]
    emb = Embedder(model="openai/test", batch_size=8, sleeper=slept.append)
    emb.embed_query("hello")
    assert len(slept) == 1
    assert 1.0 <= slept[0] <= 1.5


def test_embed_pacing_does_not_deadlock_under_low_rpm(
    _patch_litellm_embedding, monkeypatch
):
    """A tight per-minute ceiling paces work but must never hang forever."""
    monkeypatch.setattr("mewbo_graph.wiki.embedder._WINDOW_SECONDS", 0.05)
    _patch_litellm_embedding.side_effect = (
        lambda **_: _embedding_response([[0.0]])
    )
    emb = Embedder(
        model="openai/test", batch_size=1, concurrency=4, requests_per_minute=1
    )
    items = [(f"n{i}", f"t{i}") for i in range(4)]
    out = emb.embed_nodes(items)
    assert len(out) == 4


def test_embed_empty_input_returns_empty_list_without_pacer(_patch_litellm_embedding):
    emb = Embedder(model="openai/test", requests_per_minute=1, tokens_per_minute=1)
    assert emb.embed_nodes([]) == []
    _patch_litellm_embedding.assert_not_called()
