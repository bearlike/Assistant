#!/usr/bin/env python3
"""Tests for inline ``@<ref>`` context expansion at the API submit seam.

Exercise the real renderers (real files, a real git repo, real os.scandir);
only the URL fetch is stubbed at its single I/O boundary
(``attachments.parse_to_markdown``) so no network is hit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mewbo_tools.integration.reference_expansion import ReferenceExpander, expand_references

# --- file refs -------------------------------------------------------------


def test_file_ref_expands_inline(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("print('hello world')\n")
    out = ReferenceExpander(str(tmp_path)).expand("look at @sub/a.py please")
    assert "print('hello world')" in out
    assert "--- @sub/a.py ---" in out
    assert "look at" in out and "please" in out


def test_file_ref_trailing_punctuation_kept(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Title\n")
    out = ReferenceExpander(str(tmp_path)).expand("see @notes.md.")
    assert "# Title" in out
    # the sentence-ending period survives as literal text after the block
    assert out.rstrip().endswith(".")


def test_bogus_file_ref_passes_through(tmp_path: Path) -> None:
    text = "look at @does/not/exist.py here"
    assert ReferenceExpander(str(tmp_path)).expand(text) == text


# --- directory refs --------------------------------------------------------


def test_dir_ref_lists_entries(tmp_path: Path) -> None:
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "mod.py").write_text("x = 1\n")
    (d / "child").mkdir()
    out = ReferenceExpander(str(tmp_path)).expand("contents of @pkg/")
    assert "mod.py" in out
    assert "child/" in out  # directories rendered with a trailing slash
    assert "--- @pkg/ ---" in out


def test_dir_ref_without_trailing_slash(tmp_path: Path) -> None:
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "mod.py").write_text("x = 1\n")
    out = ReferenceExpander(str(tmp_path)).expand("@pkg")
    assert "mod.py" in out


# --- git diff refs ---------------------------------------------------------


def _init_repo(path: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env={**env})
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, env={**env}
    )


@pytest.mark.parametrize("token", ["@diff", "@git-diff"])
def test_diff_ref_expands(tmp_path: Path, token: str) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("one\ntwo\n")  # uncommitted change
    out = ReferenceExpander(str(tmp_path)).expand(f"review {token}")
    assert "+two" in out
    assert "f.txt" in out


def test_diff_ref_outside_repo_passes_through(tmp_path: Path) -> None:
    text = "review @diff now"
    assert ReferenceExpander(str(tmp_path)).expand(text) == text


# --- url refs --------------------------------------------------------------


def test_url_ref_expands_via_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_render(source: str) -> str:
        seen["source"] = source
        return "# Fetched\n\npage body"

    monkeypatch.setattr(
        "mewbo_tools.integration.reference_expansion.parse_to_markdown", fake_render
    )
    out = ReferenceExpander(None).expand("read @https://example.com/docs here")
    assert "page body" in out
    assert seen["source"] == "https://example.com/docs"


def test_unreachable_url_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mewbo_tools.integration.reference_expansion.parse_to_markdown", lambda s: None
    )
    text = "read @https://nope.invalid/x here"
    assert ReferenceExpander(None).expand(text) == text


# --- guardrails ------------------------------------------------------------


def test_email_is_not_a_ref(tmp_path: Path) -> None:
    text = "mail me at bob@host.com when done"
    assert ReferenceExpander(str(tmp_path)).expand(text) == text


def test_oversized_file_truncates_with_marker(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("A" * 50_000)
    out = ReferenceExpander(str(tmp_path), per_ref_chars=1_000).expand("@big.txt")
    assert "truncated to 1000 of" in out
    # body is bounded near the cap, not the full 50k
    assert len(out) < 3_000


def test_aggregate_budget_caps_later_refs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("A" * 900)
    (tmp_path / "b.txt").write_text("B" * 900)
    expander = ReferenceExpander(str(tmp_path), per_ref_chars=1_000, total_chars=900)
    out = expander.expand("@a.txt and @b.txt")
    assert "AAAA" in out  # first ref expanded (consumes the whole budget)
    assert "BBBB" not in out  # second ref had no budget left
    assert "budget reached" in out  # … and says so instead of being dropped


def test_duplicate_refs_deduped(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("UNIQUE_TOKEN = 1\n")
    out = ReferenceExpander(str(tmp_path)).expand("@a.py and again @a.py")
    assert out.count("UNIQUE_TOKEN = 1") == 1


def test_path_traversal_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("TOPSECRET\n")
    out = ReferenceExpander(str(workspace)).expand("@../secret.txt")
    assert "TOPSECRET" not in out
    assert out == "@../secret.txt"


def test_no_refs_unchanged(tmp_path: Path) -> None:
    text = "just a normal message with no references"
    assert ReferenceExpander(str(tmp_path)).expand(text) == text


def test_no_cwd_file_ref_passes_through() -> None:
    # No workspace bound → file/dir/diff cannot resolve safely.
    text = "look at @some/file.py"
    assert ReferenceExpander(None).expand(text) == text


# --- module helper ---------------------------------------------------------


def test_expand_references_helper_handles_none() -> None:
    assert expand_references(None, "/tmp") is None
    assert expand_references("plain text", "/tmp") == "plain text"


def test_expand_references_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, text):  # noqa: ANN001
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ReferenceExpander, "expand", boom)
    # On internal failure the raw text is returned, never an exception.
    assert expand_references("has @ref token", "/tmp") == "has @ref token"


# --- git-index scoping -----------------------------------------------------


def _init_empty_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def test_git_tracked_file_expands(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("X = 1\n")  # untracked-but-not-ignored
    out = ReferenceExpander(str(tmp_path)).expand("see @src/app.py")
    assert "X = 1" in out  # git ls-files --others --exclude-standard includes it


def test_gitignored_file_is_out_of_scope(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("SECRET=hunter2\n")
    out = ReferenceExpander(str(tmp_path)).expand("leak @.env please")
    assert "hunter2" not in out  # .gitignore'd → not a project file → literal
    assert out == "leak @.env please"


def test_git_dir_listing_from_index(tmp_path: Path) -> None:
    _init_empty_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("a\n")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "b.py").write_text("b\n")
    (tmp_path / "build").mkdir()
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build" / "junk.o").write_text("junk\n")
    out = ReferenceExpander(str(tmp_path)).expand("@pkg/")
    assert "a.py" in out
    assert "sub/" in out
    # the gitignored build/ dir is out of scope entirely
    assert ReferenceExpander(str(tmp_path)).expand("@build/") == "@build/"


# --- session attachments ---------------------------------------------------


def test_attachment_ref_expands(tmp_path: Path) -> None:
    # An attached file lives outside the project tree but is allow-listed.
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    (att_dir / "stored123.md").write_text("# Spec\n\nbody text\n")
    expander = ReferenceExpander(
        None, attachments={"design.md": str(att_dir / "stored123.md")}
    )
    out = expander.expand("per @design.md we should ship")
    assert "body text" in out
    assert "--- @design.md ---" in out


def test_unknown_attachment_passes_through() -> None:
    expander = ReferenceExpander(None, attachments={"design.md": "/no/such.md"})
    assert expander.expand("@nope.md") == "@nope.md"
