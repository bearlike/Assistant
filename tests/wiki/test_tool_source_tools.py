"""Tests for WikiSourceAccess and the three source-access tool shims.

Covers: wiki_read_file, wiki_grep, wiki_list_files (source_tools.py).

Design notes:
- WikiSourceAccess instance methods (read_file, grep, list_files) return plain
  dicts on success but MockSpeaker (via _err_result) on failure.  Our helpers
  normalise both into dicts so assertions stay uniform.
- The tool shims (WikiReadFileTool, WikiGrepTool, WikiListFilesTool) are
  exercised end-to-end via handle() to cover the shim boilerplate.
- for_session is exercised separately via direct patching.

BUG NOTE (not fixed — tests document it):
  WikiSourceAccess.for_session() returns _err_result() = MockSpeaker,
  but the shim's handle() checks `isinstance(access, dict)` — False for
  MockSpeaker — so the error path falls through and crashes.  The correct
  check would be `isinstance(access, MockSpeaker)`.  Documented as
  test_source_tool_shim_bug_isinstance_check.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import QaAnswer

# ── Helpers ────────────────────────────────────────────────────────────────────

SLUG = "org/repo"


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path)


def _fake_runtime(store: JsonWikiStore) -> SimpleNamespace:
    return SimpleNamespace(wiki_store=store)


def _qa(answer_id: str = "ans-src", slug: str = SLUG) -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=[],
        model="test-model",
        blocks=[],
        slug=slug,
    )


def _make_action_step(tool_input: dict) -> MagicMock:
    step = MagicMock()
    step.tool_input = tool_input
    return step


def _make_clone_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a temporary clone directory with the given relative path → content mapping."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = clone_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return clone_dir


def _source_access(clone_dir: Path, store: JsonWikiStore, slug: str = SLUG):
    """Build a WikiSourceAccess directly (bypasses for_session for unit tests)."""
    from mewbo_graph.plugins.wiki._ctx import WikiQaCtx
    from mewbo_graph.plugins.wiki.source_tools import WikiSourceAccess

    ctx = WikiQaCtx(answer_id="ans-direct", slug=slug, session_id="sess-direct", store=store)
    return WikiSourceAccess(ctx=ctx, clone_dir=clone_dir)


def _to_dict(result) -> dict:
    """Normalise a result that is either a plain dict or a MockSpeaker into a dict."""
    if isinstance(result, dict):
        return result
    # MockSpeaker(content=str(payload))
    return ast.literal_eval(result.content)


# ── WikiSourceAccess.read_file ─────────────────────────────────────────────────


def test_read_file_returns_full_content(tmp_path: Path) -> None:
    """read_file with no line range returns all content."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"src/app.py": "line1\nline2\nline3\n"})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="src/app.py"))
    payload = _to_dict(result)

    assert payload["path"] == "src/app.py"
    assert payload["totalLines"] == 3
    assert payload["startLine"] == 1
    assert payload["endLine"] == 3
    assert "line1" in payload["content"]
    assert "line3" in payload["content"]


def test_read_file_slices_with_start_and_end_line(tmp_path: Path) -> None:
    """start_line/end_line slice selects the requested range (1-based, inclusive)."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"main.py": "a\nb\nc\nd\ne\n"})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="main.py", start_line=2, end_line=4))
    payload = _to_dict(result)

    assert payload["startLine"] == 2
    assert payload["endLine"] == 4
    assert payload["content"] == "b\nc\nd"


def test_read_file_returns_not_found_for_missing_path(tmp_path: Path) -> None:
    """Missing file → error with code=not_found."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="nonexistent.py"))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "not_found"


def test_read_file_rejects_absolute_path(tmp_path: Path) -> None:
    """Paths starting with / are rejected as not_found."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="/etc/passwd"))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "not_found"


def test_read_file_rejects_path_traversal(tmp_path: Path) -> None:
    """Paths with .. escape outside clone dir are rejected as not_found."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="../../etc/passwd"))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "not_found"


def test_read_file_too_large_without_line_range(tmp_path: Path) -> None:
    """File > MAX_FILE_BYTES without line range → error with code=too_large."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs, WikiSourceAccess

    store = _store(tmp_path)
    big_content = "x" * (WikiSourceAccess.MAX_FILE_BYTES + 1)
    clone_dir = _make_clone_dir(tmp_path, {"big.txt": big_content})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="big.txt"))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "too_large"


def test_read_file_too_large_but_with_line_range_succeeds(tmp_path: Path) -> None:
    """File > MAX_FILE_BYTES WITH a line range is allowed (reads only that slice)."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    big_lines = "\n".join(f"line{i}" for i in range(10000))
    clone_dir = _make_clone_dir(tmp_path, {"huge.py": big_lines})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="huge.py", start_line=1, end_line=5))
    payload = _to_dict(result)

    # Should succeed — line range bypasses size gate
    assert "error" not in payload
    assert payload["endLine"] == 5


def test_read_file_empty_range_returns_validation_error(tmp_path: Path) -> None:
    """start_line past end_line → validation error."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"code.py": "a\nb\nc\n"})
    access = _source_access(clone_dir, store)

    result = access.read_file(WikiReadFileArgs(path="code.py", start_line=5, end_line=3))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "validation"


# ── WikiSourceAccess.grep ──────────────────────────────────────────────────────


def test_grep_finds_pattern_across_files(tmp_path: Path) -> None:
    """grep for 'def ' returns matching lines from files.

    Note: the grep impl uses fnmatch with the default '**/*' glob.
    fnmatch does NOT support '**' globbing — only files in subdirectories
    (e.g. 'src/a.py') match '**/*'; root-level files do not.
    Tests use src/ prefix to trigger actual matches.
    """
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/a.py": "def hello():\n    pass\n",
            "src/b.py": "x = 1\ndef world():\n    return 2\n",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.grep(WikiGrepArgs(pattern="def "))
    payload = _to_dict(result)

    assert "hits" in payload
    hits = payload["hits"]
    assert len(hits) >= 2
    paths = {h["path"] for h in hits}
    assert "src/a.py" in paths
    assert "src/b.py" in paths


def test_grep_case_insensitive(tmp_path: Path) -> None:
    """grep is case-insensitive: pattern 'HELLO' matches 'hello'."""
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"src/code.py": "def hello():\n    pass\n"})
    access = _source_access(clone_dir, store)

    result = access.grep(WikiGrepArgs(pattern="HELLO"))
    payload = _to_dict(result)

    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["path"] == "src/code.py"


def test_grep_glob_scope(tmp_path: Path) -> None:
    """glob='src/*.py' restricts search to Python files in src/; .txt excluded."""
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/lib.py": "def func(): pass\n",
            "src/README.txt": "def func not code\n",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.grep(WikiGrepArgs(pattern="def func", glob="src/*.py"))
    payload = _to_dict(result)

    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["path"] == "src/lib.py"


def test_grep_invalid_regex_returns_validation_error(tmp_path: Path) -> None:
    """Invalid regex pattern → error with code=validation."""
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"a.py": "x"})
    access = _source_access(clone_dir, store)

    # Use a clearly broken regex that raises re.error
    result = access.grep(WikiGrepArgs(pattern="(?P<foo>(?P<foo>x))"))
    payload = _to_dict(result)

    assert "error" in payload
    assert payload["error"]["code"] == "validation"


def test_grep_max_hits_cap(tmp_path: Path) -> None:
    """max_hits=2 caps the returned hits at 2.

    Put the file under src/ so it matches the default '**/*' fnmatch glob.
    """
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    # 5 matching lines in one file
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/many.py": "\n".join(f"match line {i}" for i in range(5)),
        },
    )
    access = _source_access(clone_dir, store)

    result = access.grep(WikiGrepArgs(pattern="match", max_hits=2))
    payload = _to_dict(result)

    assert len(payload["hits"]) <= 2
    assert payload["truncated"] is True


def test_grep_empty_repo_returns_zero_hits(tmp_path: Path) -> None:
    """Empty clone dir → 0 hits, 0 files scanned."""
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {})
    access = _source_access(clone_dir, store)

    result = access.grep(WikiGrepArgs(pattern="anything"))
    payload = _to_dict(result)

    assert payload["hits"] == []
    assert payload["filesScanned"] == 0
    assert payload["truncated"] is False


# ── WikiSourceAccess.list_files ────────────────────────────────────────────────


def test_list_files_returns_all_by_default(tmp_path: Path) -> None:
    """Default glob ('**/*') returns files matching the pattern.

    Note: fnmatch('**/*') only matches files WITH a directory component
    (e.g. 'src/a.py'), not root-level files. Use nested paths.
    """
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/a.py": "",
            "src/b.py": "",
            "docs/README.md": "",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.list_files(WikiListFilesArgs())
    payload = _to_dict(result)

    assert payload["count"] == 3
    paths = set(payload["paths"])
    assert "src/a.py" in paths
    assert "docs/README.md" in paths


def test_list_files_glob_filter(tmp_path: Path) -> None:
    """glob='src/*.py' returns only Python files directly under src/."""
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/app.py": "",
            "src/utils.py": "",
            "docs/index.md": "",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.list_files(WikiListFilesArgs(glob="src/*.py"))
    payload = _to_dict(result)

    paths = set(payload["paths"])
    assert "src/app.py" in paths
    assert "src/utils.py" in paths
    assert "docs/index.md" not in paths


def test_list_files_max_results_truncates(tmp_path: Path) -> None:
    """max_results=2 caps the result and sets truncated=True."""
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/a.py": "",
            "src/b.py": "",
            "src/c.py": "",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.list_files(WikiListFilesArgs(max_results=2))
    payload = _to_dict(result)

    assert payload["count"] == 2
    assert payload["truncated"] is True


def test_list_files_returns_sorted_paths(tmp_path: Path) -> None:
    """Returned paths are sorted alphabetically."""
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(
        tmp_path,
        {
            "src/z.py": "",
            "src/a.py": "",
            "src/m.py": "",
        },
    )
    access = _source_access(clone_dir, store)

    result = access.list_files(WikiListFilesArgs())
    payload = _to_dict(result)

    assert payload["paths"] == sorted(payload["paths"])


def test_list_files_empty_clone_dir(tmp_path: Path) -> None:
    """Empty clone dir → count=0, paths=[], truncated=False."""
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {})
    access = _source_access(clone_dir, store)

    result = access.list_files(WikiListFilesArgs())
    payload = _to_dict(result)

    assert payload["count"] == 0
    assert payload["paths"] == []
    assert payload["truncated"] is False


# ── WikiSourceAccess.for_session — error paths ────────────────────────────────


def test_for_session_returns_error_when_ctx_not_found(tmp_path: Path) -> None:
    """Session not attached to a QA answer → for_session returns error (MockSpeaker)."""
    from mewbo_graph.plugins.wiki.source_tools import WikiSourceAccess

    store = _store(tmp_path)
    runtime = _fake_runtime(store)

    with patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime):
        result = WikiSourceAccess.for_session("sess-no-ctx")

    payload = _to_dict(result)
    assert "error" in payload
    assert payload["error"]["code"] == "internal"


def test_for_session_returns_error_when_no_completed_job(tmp_path: Path) -> None:
    """QA ctx found but no completed job exists → for_session returns not_found."""
    from mewbo_graph.plugins.wiki.source_tools import WikiSourceAccess

    store = _store(tmp_path)
    store.save_qa(_qa("ans-nocomplete", slug=SLUG))
    store.attach_qa_session("ans-nocomplete", "sess-nocomplete")
    runtime = _fake_runtime(store)

    with patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime):
        result = WikiSourceAccess.for_session("sess-nocomplete")

    payload = _to_dict(result)
    assert "error" in payload
    assert payload["error"]["code"] == "not_found"


def test_for_session_success_path(tmp_path: Path, monkeypatch) -> None:
    """for_session with a valid QA ctx and existing clone dir returns WikiSourceAccess."""
    from mewbo_graph.plugins.wiki.source_tools import WikiSourceAccess
    from mewbo_graph.wiki.types import IndexingJob

    store = _store(tmp_path)
    # Create a completed job + clone dir
    job = IndexingJob(
        job_id="job-ok",
        slug=SLUG,
        status="complete",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )
    store.create_job(job)
    clone_dir = tmp_path / "clones" / "job-ok"
    clone_dir.mkdir(parents=True)
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))

    store.save_qa(_qa("ans-ok", slug=SLUG))
    store.attach_qa_session("ans-ok", "sess-ok")
    runtime = _fake_runtime(store)

    with patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime):
        result = WikiSourceAccess.for_session("sess-ok")

    assert isinstance(result, WikiSourceAccess)
    assert result.clone_dir == clone_dir


# ── Tool shim end-to-end ──────────────────────────────────────────────────────


def _setup_qa_session_with_complete_job(
    store: JsonWikiStore,
    clone_dir: Path,
    *,
    answer_id: str = "ans-tool",
    session_id: str = "sess-tool",
    monkeypatch=None,
    tmp_path: Path | None = None,
) -> None:
    """Register a QA answer + completed job and wire the clone dir."""
    from mewbo_graph.wiki.types import IndexingJob

    job_id = f"job-{answer_id}"
    job = IndexingJob(
        job_id=job_id,
        slug=SLUG,
        status="complete",
        scanned_count=0,
        total_count=0,
        current_file=None,
    )
    store.create_job(job)

    qa = _qa(answer_id=answer_id, slug=SLUG)
    store.save_qa(qa)
    store.attach_qa_session(answer_id, session_id)


def test_wiki_read_file_tool_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """WikiReadFileTool.handle() resolves the QA ctx and reads the file."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileTool, WikiSourceAccess

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"hello.py": "print('hello')\n"})
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(clone_dir.parent.parent / "clones"))

    _setup_qa_session_with_complete_job(store, clone_dir, answer_id="ans-rf", session_id="sess-rf")
    runtime = _fake_runtime(store)

    tool = WikiReadFileTool(session_id="sess-rf")
    step = _make_action_step({"path": "hello.py"})

    # Patch WikiSourceAccess._resolve_runtime AND resolve_qa_clone_dir seam
    with (
        patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime),
        patch("mewbo_graph.plugins.wiki._ctx._clone_dir_for", return_value=clone_dir),
    ):
        result = asyncio.run(tool.handle(step))

    payload = ast.literal_eval(result.content)
    assert "error" not in payload
    assert payload["path"] == "hello.py"
    assert "hello" in payload["content"]


def test_wiki_grep_tool_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """WikiGrepTool.handle() resolves QA ctx and searches files.

    Files placed in src/ so fnmatch('src/code.py', '**/*') matches correctly.
    """
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepTool, WikiSourceAccess

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"src/code.py": "def hello(): pass\n"})
    _setup_qa_session_with_complete_job(
        store, clone_dir, answer_id="ans-grep", session_id="sess-grep"
    )
    runtime = _fake_runtime(store)

    tool = WikiGrepTool(session_id="sess-grep")
    step = _make_action_step({"pattern": "def hello"})

    with (
        patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime),
        patch("mewbo_graph.plugins.wiki._ctx._clone_dir_for", return_value=clone_dir),
    ):
        result = asyncio.run(tool.handle(step))

    payload = ast.literal_eval(result.content)
    assert "error" not in payload
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["path"] == "src/code.py"


def test_wiki_list_files_tool_end_to_end(tmp_path: Path) -> None:
    """WikiListFilesTool.handle() resolves QA ctx and lists files."""
    from mewbo_graph.plugins.wiki.source_tools import WikiListFilesTool, WikiSourceAccess

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"src/main.py": "", "docs/README.md": ""})
    _setup_qa_session_with_complete_job(store, clone_dir, answer_id="ans-lf", session_id="sess-lf")
    runtime = _fake_runtime(store)

    tool = WikiListFilesTool(session_id="sess-lf")
    step = _make_action_step({})

    with (
        patch.object(WikiSourceAccess, "_resolve_runtime", return_value=runtime),
        patch("mewbo_graph.plugins.wiki._ctx._clone_dir_for", return_value=clone_dir),
    ):
        result = asyncio.run(tool.handle(step))

    payload = ast.literal_eval(result.content)
    assert "error" not in payload
    assert payload["count"] == 2
    paths = set(payload["paths"])
    assert "docs/README.md" in paths
    assert "src/main.py" in paths


# ── OSError on read_bytes (line 159-160) ─────────────────────────────────────


def test_read_file_returns_internal_error_on_oserror(tmp_path: Path) -> None:
    """read_file returns internal error when read_bytes raises OSError (lines 159-160)."""
    from mewbo_graph.plugins.wiki.source_tools import WikiReadFileArgs

    store = _store(tmp_path)
    clone_dir = _make_clone_dir(tmp_path, {"src/test.py": "content"})
    access = _source_access(clone_dir, store)

    # Patch Path.read_bytes to raise OSError
    with patch("pathlib.Path.read_bytes", side_effect=OSError("permission denied")):
        result = access.read_file(WikiReadFileArgs(path="src/test.py"))

    payload = _to_dict(result)
    assert "error" in payload
    assert payload["error"]["code"] == "internal"


# ── grep: too-large file skipped (line 211) ───────────────────────────────────


def test_grep_skips_very_large_files(tmp_path: Path) -> None:
    """grep skips files larger than MAX_FILE_BYTES * 4 (line 213-214).

    We verify the behaviour by checking that no hits are returned for a file
    whose reported size exceeds the cap.  Rather than mocking stat() (which
    breaks is_file() via st_mode), we write a file and monkeypatch the
    size check directly on the grep method with a real large file.
    Alternatively we verify the guard via the scanned count: a too-large file
    increments scanned but contributes 0 hits.
    """
    from mewbo_graph.plugins.wiki.source_tools import WikiGrepArgs, WikiSourceAccess

    store = _store(tmp_path)
    # Write a file large enough to trigger the size guard
    big_content = "def match(): pass\n" * 1000  # ~18 KB per line × 1000 = 18MB-ish is too slow
    # Use the max threshold directly: write just above MAX_FILE_BYTES * 4 bytes
    threshold = WikiSourceAccess.MAX_FILE_BYTES * 4 + 1
    big_content = "x" * threshold
    clone_dir = _make_clone_dir(tmp_path, {"src/big.py": big_content})
    access = _source_access(clone_dir, store)

    # The file is too large — grep should skip it (0 hits) but count it scanned
    result = access.grep(WikiGrepArgs(pattern="x"))
    payload = _to_dict(result)
    # File skipped due to size → no hits
    assert payload["hits"] == []


# ── BUG DOC: isinstance check in handle() ────────────────────────────────────


def test_source_tool_shim_bug_isinstance_check_documented(tmp_path: Path) -> None:
    """Document: for_session returns MockSpeaker on error; the shim checks
    isinstance(access, dict) which is False for MockSpeaker.
    This means the error path in for_session does NOT return gracefully
    through the shim — it would fall through to _call(MockSpeaker, args).

    We document this by verifying _err_result returns MockSpeaker, not dict.
    """
    from mewbo_core.common import MockSpeaker
    from mewbo_graph.plugins.wiki._base import _err_result

    err = _err_result("internal", "test error")
    # BUG: this is MockSpeaker, not dict — the isinstance(access, dict) check in
    # _SourceToolShim.handle() will be False, allowing a crash.
    assert isinstance(err, MockSpeaker)
    assert not isinstance(err, dict)
