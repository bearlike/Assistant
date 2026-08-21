"""Tests for WikiScanTreeTool — TDD: tests written before implementation."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.plugins.wiki.scan import WikiScanTreeTool
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

# ── Fixture path ────────────────────────────────────────────────────────────


TINY_REPO = Path(__file__).parent / "fixtures" / "tiny_repo"

COMMIT = "c0ffee1234567890"


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
        commit_sha=COMMIT,
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
    slug: str = "org/repo",
    store: JsonWikiStore | None = None,
) -> tuple:
    """Set up store + tool, run scan, return (result, store, job_id)."""
    import mewbo_graph.plugins.wiki.scan as scan_mod

    store = store if store is not None else _store(tmp_path)
    job = _job(job_id, slug)
    store.create_job(job)
    store.attach_job_session(job_id, session_id)

    runtime = _fake_runtime(store)
    tool = WikiScanTreeTool(session_id=session_id)

    # _clone_dir_for lives in _ctx and is called by resolve_job_ctx — patch it there.
    with patch.object(scan_mod, "_resolve_runtime", return_value=runtime), \
         patch("mewbo_graph.plugins.wiki._ctx._clone_dir_for", return_value=clone_dir):
        result = asyncio.run(tool.handle(_make_action_step(tool_input)))

    return result, store, job_id


def _payload(result) -> dict:
    """Parse the tool's result content back into a dict."""
    assert "error" not in result.content, f"Unexpected error: {result.content}"
    return ast.literal_eval(result.content)


def _scanned_paths(store: JsonWikiStore, slug: str = "org/repo") -> list[str]:
    """The paths the scan actually walked, read off the persisted manifest."""
    return [m.path for m in store.list_file_manifest(slug)]


def _build_repo(root: Path, paths: list[str], *, body: str = "x") -> Path:
    """Materialise a synthetic repo of *paths* under *root*."""
    for rel in paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root


def _uniform_repo(root: Path, count: int) -> Path:
    """A repo of *count* files with a FIXED shape: 10 dirs, 2 extensions.

    Shape is held constant so a size comparison across two counts measures the
    only thing that differs — the number of files.
    """
    exts = (".py", ".md")
    return _build_repo(
        root,
        [
            f"pkg{i % 10}/sub/mod_{i}{exts[i % 2]}"
            for i in range(count)
        ],
    )


# ── Test 1: default excludes .git and node_modules ───────────────────────────


def test_scan_default_excludes_dotgit_and_node_modules(
    tmp_path: Path,
) -> None:
    """Scan with exclude mode + empty filters; node_modules/.gitkeep is excluded."""
    result, store, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
    )

    payload = _payload(result)
    paths = _scanned_paths(store)

    # node_modules/.gitkeep must be excluded
    assert not any("node_modules" in p for p in paths)
    # .git internals must not appear
    assert not any(".git" in p for p in paths)
    # should have at least 9 files (README, pyproject, package-lock, src x3, tests x2, docs x1)
    assert len(paths) >= 9
    assert payload["totals"]["files"] == len(paths)


# ── Test 2: exclude filter drops matched dirs and files ──────────────────────


def test_scan_exclude_filter_drops_matched(tmp_path: Path) -> None:
    """Exclude mode: dirs=['tests'], files=['package-lock.json'] drops those entries."""
    _, store_all, _ = _run_scan(
        tmp_path / "all",
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-all",
        session_id="sess-all",
    )
    _, store_filtered, _ = _run_scan(
        tmp_path / "filtered",
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": ["tests"], "files": ["package-lock.json"]},
        job_id="job-excl",
        session_id="sess-excl",
    )

    all_paths = set(_scanned_paths(store_all))
    filtered_paths = set(_scanned_paths(store_filtered))

    # tests/ dir and package-lock.json should be gone
    assert not any("tests" in p.split("/") for p in filtered_paths)
    assert not any(p.endswith("package-lock.json") for p in filtered_paths)
    # count is smaller
    assert len(filtered_paths) < len(all_paths)


# ── Test 3: include filter keeps only matched ─────────────────────────────────


def test_scan_include_filter_keeps_only_matched(tmp_path: Path) -> None:
    """Include mode with dirs=['src'] returns only files whose path includes 'src'."""
    _, store, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "include", "dirs": ["src"], "files": []},
        job_id="job-incl",
        session_id="sess-incl",
    )

    paths = _scanned_paths(store)

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

    total = _payload(result)["totals"]["files"]

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
    _, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-curfile",
        session_id="sess-curfile",
    )

    last_path = _scanned_paths(store)[-1]  # already sorted

    job = store.get_job(job_id)
    assert job is not None
    assert job.current_file == last_path


# ── Test 6: manifest is sorted ───────────────────────────────────────────────


def test_scan_completion_log_is_owned_by_manifest_persistence(tmp_path: Path) -> None:
    """The scan's terminal count is attributed to the persistence it summarizes."""
    _, store, job_id = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-scan-log",
        session_id="sess-scan-log",
    )

    logs = [event for event in store.load_job_events(job_id) if event["type"] == "log"]
    complete = next(event for event in logs if event["text"].startswith("Scanned "))
    assert complete["step"] == "scan.persist_manifest"


def test_scan_persists_sorted_manifest(tmp_path: Path) -> None:
    """Persisted manifest paths are lexicographically sorted."""
    _, store, _ = _run_scan(
        tmp_path,
        TINY_REPO,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-sort",
        session_id="sess-sort",
    )

    paths = _scanned_paths(store)
    assert paths == sorted(paths)


# ── Test 7: the returned result is bounded, not a per-file inventory ─────────


def test_result_is_bounded_and_does_not_scale_with_file_count(tmp_path: Path) -> None:
    """A 10x bigger repo of the same shape costs the model the same context.

    Returning every ``{path, size, ext}`` dict costs ~180,000 characters on a
    real repository — within 10% of the session-tool result cap, so a slightly
    larger repo truncates mid-manifest.
    """
    small_result, _, _ = _run_scan(
        tmp_path / "small-store",
        _uniform_repo(tmp_path / "small", 50),
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-small",
        session_id="sess-small",
    )
    big_result, _, _ = _run_scan(
        tmp_path / "big-store",
        _uniform_repo(tmp_path / "big", 500),
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-big",
        session_id="sess-big",
    )

    small_len = len(small_result.content)
    big_len = len(big_result.content)

    # Absolute ceiling — and the tool's own declared cap is never approached.
    assert big_len < 4_000
    assert big_len < WikiScanTreeTool.max_result_chars

    # 10x the files must not cost meaningfully more context. A linear payload
    # would be ~10x here; the fold only widens the rendered counters.
    assert big_len < small_len * 1.2, (
        f"result grew with file count: {small_len} -> {big_len}"
    )

    # Still honest about how much it scanned.
    assert _payload(small_result)["totals"]["files"] == 50
    assert _payload(big_result)["totals"]["files"] == 500


def test_result_stays_bounded_when_every_group_cap_is_saturated(tmp_path: Path) -> None:
    """A wide repo saturates every cap and still fits, reporting the true totals."""
    paths = [
        f"top{i}/sub{i}/file_{i}.e{i}"
        for i in range(60)
    ] + [f"root_{i}.txt" for i in range(40)]
    result, _, _ = _run_scan(
        tmp_path / "wide-store",
        _build_repo(tmp_path / "wide", paths),
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-wide",
        session_id="sess-wide",
    )

    payload = _payload(result)

    assert len(result.content) < WikiScanTreeTool.max_result_chars
    # Lists are capped...
    assert len(payload["extensions"]) == 15
    assert len(payload["directories"]) == 30
    assert len(payload["root_files"]) == 20
    # ...but the totals disclose what was elided, so the truncation is visible.
    assert payload["totals"]["files"] == 100
    assert payload["totals"]["extensions"] == 61  # 60 distinct .eN + .txt
    assert payload["totals"]["directories"] == 61  # 60 top/sub groups + "."


# ── Test 8: the summary carries what a page planner needs ────────────────────


def test_summary_carries_the_facts_a_planner_needs(tmp_path: Path) -> None:
    """Counts, byte totals, an extension histogram, a directory breakdown, a pointer."""
    repo = _build_repo(
        tmp_path / "planner",
        [
            "README.md",
            "pyproject.toml",
            "packages/core/a.py",
            "packages/core/b.py",
            "packages/core/c.py",
            "packages/graph/d.py",
            "apps/api/e.py",
            "apps/web/f.ts",
        ],
        body="0123456789",
    )
    result, _, _ = _run_scan(
        tmp_path / "planner-store",
        repo,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-planner",
        session_id="sess-planner",
    )

    payload = _payload(result)

    assert payload["totals"]["files"] == 8
    assert payload["totals"]["bytes"] == 80

    exts = {e["ext"]: e["files"] for e in payload["extensions"]}
    assert exts[".py"] == 5
    assert exts[".ts"] == 1
    assert exts[".md"] == 1

    dirs = {d["dir"]: d["files"] for d in payload["directories"]}
    assert dirs["packages/core"] == 3
    assert dirs["packages/graph"] == 1
    assert dirs["apps/api"] == 1
    assert dirs["apps/web"] == 1
    assert dirs["."] == 2  # root files group under "."

    # Root files are named — the cheapest ecosystem signal a planner has.
    assert set(payload["root_files"]) == {"README.md", "pyproject.toml"}

    # An explicit, actionable pointer to the detail the result no longer carries.
    detail = payload["detail"]
    for tool_name in ("ls", "glob", "grep", "read_file"):
        assert tool_name in detail


def test_root_files_are_not_crowded_out_by_dotfiles(tmp_path: Path) -> None:
    """Dotfiles sort first; they must not spend the whole root-file budget.

    Measured on a real tree, 14 of the 20 slots went to ``.gitignore``-class
    entries and editor droppings before the fold reached ``README.md``.
    """
    dotfiles = [f".cfg{i:02d}" for i in range(30)]
    repo = _build_repo(
        tmp_path / "dotty",
        [*dotfiles, "README.md", "pyproject.toml"],
    )
    result, _, _ = _run_scan(
        tmp_path / "dotty-store",
        repo,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-dotty",
        session_id="sess-dotty",
    )

    payload = _payload(result)
    assert payload["root_files"] == ["README.md", "pyproject.toml"]
    # Nothing is hidden — the dotfiles are still counted.
    assert payload["totals"]["files"] == 32
    dirs = {d["dir"]: d["files"] for d in payload["directories"]}
    assert dirs["."] == 32


# ── Test 9: the manifest is persisted, readable back, and commit-stamped ─────


def test_manifest_is_persisted_with_hashes_and_commit(tmp_path: Path) -> None:
    """Every scanned file lands in the store's manifest, stamped with the commit."""
    repo = _build_repo(
        tmp_path / "repo",
        ["a.py", "pkg/b.py"],
        body="hello",
    )
    _, store, _ = _run_scan(
        tmp_path / "store",
        repo,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-manifest",
        session_id="sess-manifest",
        slug="org/repo",
    )

    entries = {m.path: m for m in store.list_file_manifest("org/repo")}
    assert set(entries) == {"a.py", "pkg/b.py"}

    import hashlib

    expected = hashlib.sha256(b"hello").hexdigest()
    for entry in entries.values():
        assert entry.slug == "org/repo"
        assert entry.content_hash == expected
        assert entry.last_indexed_commit == COMMIT


def test_manifest_hash_is_the_one_change_detector_diffs_against(tmp_path: Path) -> None:
    """A re-scan of an unchanged tree reads as clean; an edited file reads as dirty.

    The manifest exists to make an incremental refresh possible, so the property
    that matters is not "a hash was written" but that ``ChangeDetector`` — the
    only reader — agrees with it.
    """
    from mewbo_graph.wiki.refresh import ChangeDetector

    repo = _build_repo(tmp_path / "repo", ["a.py", "b.py"], body="one")
    _, store, _ = _run_scan(
        tmp_path / "store",
        repo,
        {"filter_mode": "exclude", "dirs": [], "files": []},
        job_id="job-diff",
        session_id="sess-diff",
    )

    files = [repo / "a.py", repo / "b.py"]
    clean = ChangeDetector(store).detect("org/repo", repo, files)
    assert clean.is_empty, f"unchanged tree read as dirty: {clean}"

    (repo / "a.py").write_text("two")
    dirty = ChangeDetector(store).detect("org/repo", repo, files)
    assert dirty.modified == ["a.py"]
    assert dirty.added == []
    assert dirty.deleted == []


# ── Test 10: current_file writes are throttled, not one-per-file ─────────────


def test_current_file_updates_are_throttled_not_one_per_file(tmp_path: Path) -> None:
    """``update_job`` runs on the flush cadence, not once per scanned file.

    It used to fire unconditionally per file — ~2,000 store round-trips on a
    real repository for a field the UI merely samples.
    """
    store = _store(tmp_path / "store")
    spy = MagicMock(side_effect=store.update_job)
    repo = _uniform_repo(tmp_path / "repo", 500)

    with patch.object(store, "update_job", spy):
        _, _, job_id = _run_scan(
            tmp_path / "unused",
            repo,
            {"filter_mode": "exclude", "dirs": [], "files": []},
            job_id="job-throttle",
            session_id="sess-throttle",
            store=store,
        )

    progress_writes = [c for c in spy.call_args_list if "current_file" in c.kwargs]
    assert len(progress_writes) < 50, (
        f"expected throttled writes, got {len(progress_writes)} for 500 files"
    )
    # Every one carries the WHOLE progress payload — each field rides this one
    # flush rather than owning a write of its own. That is the invariant (one
    # round-trip per flush), not the field list: ``last_progress_at`` was put
    # here precisely BECAUSE this write already happens, so scan reports
    # liveness without adding a round-trip.
    for call in progress_writes:
        assert set(call.kwargs) == {
            "scanned_count", "current_file", "last_progress_at",
        }

    # Live progress is still meaningful: the finished job names its last file.
    job = store.get_job(job_id)
    assert job is not None
    assert job.scanned_count == 500
    assert job.current_file == _scanned_paths(store)[-1]
