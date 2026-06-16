"""Tests for the CLI prompt completer (``@`` files, ``/`` commands + skills)."""

# ruff: noqa: I001
import subprocess
from dataclasses import dataclass

from prompt_toolkit.document import Document

from mewbo_cli.cli_completer import MewboCompleter


@dataclass
class _FakeSkill:
    """Minimal SkillSpec stand-in for completer tests."""

    name: str
    user_invocable: bool = True


class _FakeSkillRegistry:
    """Duck-typed SkillRegistry exposing only ``list_user_invocable``."""

    def __init__(self, skills):
        self._skills = list(skills)

    def list_user_invocable(self, session_capabilities=()):
        return [s for s in self._skills if s.user_invocable]


def _git_repo(tmp_path, files):
    """Create a tmp git repo containing ``files`` and return its path."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return str(tmp_path)


def _complete(completer, text):
    doc = Document(text=text, cursor_position=len(text))
    return list(completer.get_completions(doc, None))


def test_mention_suggests_project_files(tmp_path):
    repo = _git_repo(tmp_path, ["src/app.py", "src/util.py", "README.md"])
    completer = MewboCompleter([], None, cwd_provider=lambda: repo)

    completions = _complete(completer, "look at @src/ap")
    texts = [c.text for c in completions]
    assert "src/app.py" in texts
    assert "README.md" not in texts
    assert all(c.display_meta_text == "file" for c in completions)
    # Replaces the partial after the "@".
    assert completions[0].start_position == -len("src/ap")


def test_mention_substring_match(tmp_path):
    repo = _git_repo(tmp_path, ["src/app.py", "docs/util.md"])
    completer = MewboCompleter([], None, cwd_provider=lambda: repo)

    texts = [c.text for c in _complete(completer, "@util")]
    assert "docs/util.md" in texts


def test_slash_suggests_commands_and_skills(tmp_path):
    skills = _FakeSkillRegistry(
        [_FakeSkill("summon-helper"), _FakeSkill("hidden", user_invocable=False)]
    )
    completer = MewboCompleter(
        ["/summary", "/status", "/help"], skills, cwd_provider=lambda: str(tmp_path)
    )

    completions = _complete(completer, "/su")
    by_text = {c.text: c.display_meta_text for c in completions}
    assert by_text.get("summary") == "command"
    assert by_text.get("summon-helper") == "skill"
    assert "status" not in by_text  # does not match "su"
    assert "hidden" not in by_text  # not user-invocable
    assert completions[0].start_position == -len("su")


def test_slash_only_at_line_start_word(tmp_path):
    completer = MewboCompleter(["/help"], None, cwd_provider=lambda: str(tmp_path))
    # A space after the slash word ends slash mode.
    assert _complete(completer, "/help ") == []


def test_plain_text_yields_no_completions(tmp_path):
    repo = _git_repo(tmp_path, ["src/app.py"])
    completer = MewboCompleter(["/help"], None, cwd_provider=lambda: repo)
    assert _complete(completer, "just some words") == []


def test_email_like_at_does_not_trigger(tmp_path):
    repo = _git_repo(tmp_path, ["src/app.py"])
    completer = MewboCompleter([], None, cwd_provider=lambda: repo)
    # "@b" follows a word char, so it is not an active mention token.
    assert _complete(completer, "email a@b") == []


def test_get_completions_never_raises(tmp_path):
    def boom():
        raise RuntimeError("no cwd")

    completer = MewboCompleter([], None, cwd_provider=boom)
    # A failing cwd provider degrades to no file suggestions, never an exception.
    assert _complete(completer, "@src/ap") == []
