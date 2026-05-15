"""Tests for WikiScanTreeTool — TDD: tests written before implementation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob

# ── Fixture path ────────────────────────────────────────────────────────────


TINY_REPO = Path(__file__).parent / "fixtures" / "tiny_repo"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _job(job_id: str = "job-scan", slug: str = "org/repo") -> IndexingJob:
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


def _run_scan(
    tmp_path: Path,
    clone_dir: Path,
    tool_input: dict,
    *,
    job_id: str = "job-scan1",
    session_id: str = "sess-scan1",
) -> tuple:
    """Set up store + tool, run scan, return (result, store, job_id)."""
    import mewbo_core.builtin_plugins.wiki.scan as scan_mod
    from mewbo_core.builtin_plugins.wiki.scan import WikiScanTreeTool

    store = _store(tmp_path)
    job = _job(job_id)
    store.create_job(job)
    store.attach_job_session(job_id, session_id)

    runtime = _fake_runtime(store)
    tool = WikiScanTreeTool(session_id=session_id)

    # _clone_dir_for lives in _ctx and is called by resolve_job_ctx — patch it there.
    with patch.object(scan_mod, "_resolve_runtime", return_value=runtime), \
         patch("mewbo_core.builtin_plugins.wiki._ctx._clone_dir_for", return_value=clone_dir):
        result = asyncio.run(tool.handle(_make_action_step(tool_input)))

    return result, store, job_id


# ── Test 1: default excludes .git and node_modules ───────────────────────────


def test_scan_default_excludes_dotgit_and_node_modules(
    tmp_path: Path,
) -> None:
    """Scan with exclude mode + empty filters; node_modules/.gitkeep is excluded."""
    result, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
    )

    assert "error" not in result.content, f"Unexpected error: {result.content}"
    import ast
    payload = ast.literal_eval(result.content)
    files = payload["files"]
    paths = [f["path"] for f in files]

    # node_modules/.gitkeep must be excluded
    assert not any("node_modules" in p for p in paths)
    # .git internals must not appear
    assert not any(".git" in p for p in paths)
    # should have at least 9 files (README, pyproject, package-lock, src x3, tests x2, docs x1)
    assert len(files) >= 9


# ── Test 2: exclude filter drops matched dirs and files ──────────────────────


def test_scan_exclude_filter_drops_matched(tmp_path: Path) -> None:
    """Exclude mode: dirs=['tests'], files=['package-lock.json'] drops those entries."""
    result_all, _, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-all",
        session_id="sess-all",
    )
    result_filtered, _, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": ["tests"], "files": ["package-lock.json"]},
        job_id="job-excl",
        session_id="sess-excl",
    )

    import ast
    all_paths = {f["path"] for f in ast.literal_eval(result_all.content)["files"]}
    filtered_paths = {f["path"] for f in ast.literal_eval(result_filtered.content)["files"]}

    # tests/ dir and package-lock.json should be gone
    assert not any("tests" in p.split("/") for p in filtered_paths)
    assert not any(p.endswith("package-lock.json") for p in filtered_paths)
    # count is smaller
    assert len(filtered_paths) < len(all_paths)


# ── Test 3: include filter keeps only matched ─────────────────────────────────


def test_scan_include_filter_keeps_only_matched(tmp_path: Path) -> None:
    """Include mode with dirs=['src'] returns only files whose path includes 'src'."""
    result, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "include", "dirs": ["src"], "files": []},
        job_id="job-incl",
        session_id="sess-incl",
    )

    import ast
    payload = ast.literal_eval(result.content)
    files = payload["files"]
    paths = [f["path"] for f in files]

    assert len(paths) > 0
    for p in paths:
        parts = Path(p).parts
        assert "src" in parts, f"Expected 'src' in path segments of: {p}"


# ── Test 4: scanning + scanned events per file ───────────────────────────────


def test_scan_emits_scanning_and_scanned_per_file(tmp_path: Path) -> None:
    """One 'scanning' and one 'scanned' event per included file; monotonic index."""
    result, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-events",
        session_id="sess-events",
    )

    import ast
    payload = ast.literal_eval(result.content)
    total = len(payload["files"])

    events = store.load_job_events(job_id)
    scanning_evts = [e for e in events if e["type"] == "scanning"]
    scanned_evts = [e for e in events if e["type"] == "scanned"]

    assert len(scanning_evts) == total, (
        f"Expected {total} scanning events, got {len(scanning_evts)}"
    )
    assert len(scanned_evts) == total, (
        f"Expected {total} scanned events, got {len(scanned_evts)}"
    )

    # Indices are monotonically increasing within each event type
    scanning_indices = [e["index"] for e in scanning_evts]
    scanned_indices = [e["index"] for e in scanned_evts]
    assert scanning_indices == list(range(total))
    assert scanned_indices == list(range(total))

    # totalCount is consistent
    for e in scanning_evts + scanned_evts:
        assert e["totalCount"] == total


# ── Test 5: currentFile updated on the job ───────────────────────────────────


def test_scan_updates_current_file(tmp_path: Path) -> None:
    """After the scan the job's currentFile is the last included file (lexicographic)."""
    result, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-curfile",
        session_id="sess-curfile",
    )

    import ast
    payload = ast.literal_eval(result.content)
    last_path = payload["files"][-1]["path"]  # already sorted

    job = store.get_job(job_id)
    assert job is not None
    assert job.current_file == last_path


# ── Test 6: manifest is sorted ───────────────────────────────────────────────


def test_scan_returns_sorted_manifest(tmp_path: Path) -> None:
    """Returned manifest paths are lexicographically sorted."""
    result, _, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-sort",
        session_id="sess-sort",
    )

    import ast
    paths = [f["path"] for f in ast.literal_eval(result.content)["files"]]
    assert paths == sorted(paths)
