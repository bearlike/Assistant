"""Tests for WikiLoadGrounderTool — TDD: tests written before implementation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob

# ── Helpers ───────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-grd", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="scanning",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _run_grounder(
    tmp_path: Path,
    clone_dir: Path,
    tool_input: dict | None = None,
    *,
    job_id: str = "job-grd1",
    session_id: str = "sess-grd1",
) -> tuple:
    """Set up store + tool, run grounder, return (result, store, job_id)."""
    import mewbo_core.builtin_plugins.wiki.grounder as grounder_mod
    from mewbo_core.builtin_plugins.wiki.grounder import WikiLoadGrounderTool

    store = _store(tmp_path)
    job = _job(job_id)
    store.create_job(job)
    store.attach_job_session(job_id, session_id)

    runtime = _fake_runtime(store)
    tool = WikiLoadGrounderTool(session_id=session_id)

    with patch.object(grounder_mod, "_resolve_runtime", return_value=runtime), \
         patch("mewbo_core.builtin_plugins.wiki._ctx._clone_dir_for", return_value=clone_dir):
        result = asyncio.run(tool.handle(_make_action_step(tool_input or {})))

    return result, store, job_id


_VALID_GROUNDER = {
    "repo_notes": [{"content": "This repo uses FastAPI."}],
    "pages": [
        {"title": "Overview", "purpose": "High-level description", "parent": None},
        {"title": "API Reference", "purpose": "REST endpoints", "parent": "Overview"},
    ],
}


# ── Test 1: loads .mewbo/wiki.json ────────────────────────────────────────────


def test_grounder_loads_mewbo_wiki_json(tmp_path: Path) -> None:
    """Write .mewbo/wiki.json; tool returns parsed grounder with source_path."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    mewbo_dir = clone_dir / ".mewbo"
    mewbo_dir.mkdir()
    (mewbo_dir / "wiki.json").write_text(json.dumps(_VALID_GROUNDER), encoding="utf-8")

    result, _, _ = _run_grounder(tmp_path, clone_dir)

    import ast
    payload = ast.literal_eval(result.content)
    assert payload["grounder"] is not None
    assert payload["source_path"] == ".mewbo/wiki.json"
    assert len(payload["grounder"]["pages"]) == 2
    assert payload["grounder"]["repo_notes"][0]["content"] == "This repo uses FastAPI."


# ── Test 2: falls back to .devin/wiki.json ────────────────────────────────────


def test_grounder_falls_back_to_devin_wiki_json(tmp_path: Path) -> None:
    """Only .devin/wiki.json present; tool returns it."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    devin_dir = clone_dir / ".devin"
    devin_dir.mkdir()
    (devin_dir / "wiki.json").write_text(json.dumps(_VALID_GROUNDER), encoding="utf-8")

    result, _, _ = _run_grounder(
        tmp_path, clone_dir, job_id="job-devin", session_id="sess-devin"
    )

    import ast
    payload = ast.literal_eval(result.content)
    assert payload["grounder"] is not None
    assert payload["source_path"] == ".devin/wiki.json"


# ── Test 3: prefers .mewbo over .devin ───────────────────────────────────────


def test_grounder_prefers_mewbo_over_devin_when_both_present(tmp_path: Path) -> None:
    """Both files present: .mewbo/wiki.json wins; source_path reflects it."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / ".mewbo").mkdir()
    (clone_dir / ".devin").mkdir()
    (clone_dir / ".mewbo" / "wiki.json").write_text(
        json.dumps({**_VALID_GROUNDER, "repo_notes": [{"content": "from mewbo"}]}),
        encoding="utf-8",
    )
    (clone_dir / ".devin" / "wiki.json").write_text(
        json.dumps({**_VALID_GROUNDER, "repo_notes": [{"content": "from devin"}]}),
        encoding="utf-8",
    )

    result, _, _ = _run_grounder(
        tmp_path, clone_dir, job_id="job-both", session_id="sess-both"
    )

    import ast
    payload = ast.literal_eval(result.content)
    assert payload["source_path"] == ".mewbo/wiki.json"
    assert payload["grounder"]["repo_notes"][0]["content"] == "from mewbo"


# ── Test 4: returns null when neither present ──────────────────────────────────


def test_grounder_returns_null_when_neither_present(tmp_path: Path) -> None:
    """Empty clone dir: grounder is null."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    result, _, _ = _run_grounder(
        tmp_path, clone_dir, job_id="job-null", session_id="sess-null"
    )

    import ast
    payload = ast.literal_eval(result.content)
    assert payload["grounder"] is None


# ── Test 5: validation error on malformed JSON ────────────────────────────────


def test_grounder_returns_validation_error_on_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON in .mewbo/wiki.json → error with code=validation."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / ".mewbo").mkdir()
    (clone_dir / ".mewbo" / "wiki.json").write_text("{not valid json", encoding="utf-8")

    result, _, _ = _run_grounder(
        tmp_path, clone_dir, job_id="job-badjson", session_id="sess-badjson"
    )

    import ast
    payload = ast.literal_eval(result.content)
    assert "error" in payload
    assert payload["error"]["code"] == "validation"


# ── Test 6: schema violation → validation error ───────────────────────────────


def test_grounder_validation_error_on_schema_violation(tmp_path: Path) -> None:
    """pages is a string (not a list) → WikiGrounder validation fails → code=validation."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / ".mewbo").mkdir()
    (clone_dir / ".mewbo" / "wiki.json").write_text(
        json.dumps({"pages": "not a list"}), encoding="utf-8"
    )

    result, _, _ = _run_grounder(
        tmp_path, clone_dir, job_id="job-schema", session_id="sess-schema"
    )

    import ast
    payload = ast.literal_eval(result.content)
    assert "error" in payload
    assert payload["error"]["code"] == "validation"
