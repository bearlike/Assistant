"""Shared test doubles for the wiki refresh subsystem."""
from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.wiki.graph import GraphParseResult


@pytest.fixture(autouse=True)
def _no_ambient_git_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the DEV MACHINE's real git credentials during wiki tests.

    ``resolve_chain`` consults ``ambient_credential`` (a real ``git credential
    fill`` subprocess) as its penultimate candidate — so any test that reaches
    clone/finalize/freshness/branch resolution would otherwise read (and inject)
    the developer's ACTUAL host credentials. That is both non-deterministic and a
    secret-leak risk. Default it to "no ambient credential" for every wiki test; a
    test that exercises the ambient tier overrides this with its own patch inside
    the test body (the local monkeypatch wins over this autouse setup).
    """
    monkeypatch.setattr(
        "mewbo_graph.wiki.credentials.ambient_credential", lambda host: None
    )


class FakeParser:
    """Stub GraphIndex: returns a canned GraphParseResult per relative path."""

    def __init__(self, results: dict[str, GraphParseResult]) -> None:
        self.results = results

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        rel = str(file_path.relative_to(repo_root))
        return self.results.get(rel, GraphParseResult(nodes=[], edges=[], skipped=[rel]))
