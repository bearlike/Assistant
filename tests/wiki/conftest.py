"""Shared test doubles for the wiki refresh subsystem."""
from __future__ import annotations

from pathlib import Path

from mewbo_graph.wiki.graph import GraphParseResult


class FakeParser:
    """Stub GraphIndex: returns a canned GraphParseResult per relative path."""

    def __init__(self, results: dict[str, GraphParseResult]) -> None:
        self.results = results

    def parse_file(
        self, slug: str, file_path: Path, *, repo_root: Path
    ) -> GraphParseResult:
        rel = str(file_path.relative_to(repo_root))
        return self.results.get(rel, GraphParseResult(nodes=[], edges=[], skipped=[rel]))
