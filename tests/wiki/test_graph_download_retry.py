"""Resilience tests for the tree-sitter parser download path.

Regression: ``_load_ts_language`` fetches parser shared libraries on demand
from a GitHub release on a cold cache. A single transient upstream 5xx (a 504
was observed in production) aborted an entire multi-minute wiki indexing run
because the download was issued exactly once with no retry. ``_download_with_retry``
turns a transient failure into a short bounded-backoff wait; a genuine, repeated
failure still surfaces (the last exception is re-raised).

These exercise the real retry policy with an injected ``sleep`` so the test is
deterministic and fast — the production behaviour (delays + re-raise + call
counts) breaks if the retry loop is removed, so this is not a namesake test.
"""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.graph import _download_with_retry


class _FlakyDownloader:
    """Fake ``tlp.download`` that fails ``fail_times`` then succeeds."""

    def __init__(self, fail_times: int, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.calls: list[list[str]] = []
        self._exc = exc or RuntimeError("Download error: http status: 504")

    def __call__(self, langs: list[str]) -> None:
        self.calls.append(langs)
        if len(self.calls) <= self.fail_times:
            raise self._exc


def test_succeeds_first_try_no_sleep() -> None:
    dl = _FlakyDownloader(fail_times=0)
    slept: list[float] = []

    _download_with_retry(dl, "python", sleep=slept.append)

    assert dl.calls == [["python"]]
    assert slept == []  # no retry → no backoff


def test_retries_transient_then_succeeds() -> None:
    dl = _FlakyDownloader(fail_times=2)
    slept: list[float] = []

    _download_with_retry(
        dl, "go", attempts=3, base_delay=2.0, max_delay=30.0, sleep=slept.append
    )

    # 2 failures + 1 success = 3 calls; 2 backoff sleeps between them.
    assert dl.calls == [["go"], ["go"], ["go"]]
    assert slept == [2.0, 4.0]  # capped exponential: base*2**0, base*2**1


def test_exhausts_attempts_then_reraises_last_exception() -> None:
    boom = RuntimeError("Download error: http status: 504")
    dl = _FlakyDownloader(fail_times=99, exc=boom)
    slept: list[float] = []

    with pytest.raises(RuntimeError) as ei:
        _download_with_retry(dl, "rust", attempts=3, sleep=slept.append)

    assert ei.value is boom
    assert len(dl.calls) == 3  # exactly ``attempts`` tries, no more
    assert len(slept) == 2  # one sleep between each of the 3 attempts


def test_backoff_is_capped_at_max_delay() -> None:
    dl = _FlakyDownloader(fail_times=99)
    slept: list[float] = []

    with pytest.raises(RuntimeError):
        _download_with_retry(
            dl, "python", attempts=5, base_delay=10.0, max_delay=15.0,
            sleep=slept.append,
        )

    # base*2**n = 10, 20, 40, 80 → capped to 15 from the 2nd backoff on.
    assert slept == [10.0, 15.0, 15.0, 15.0]
