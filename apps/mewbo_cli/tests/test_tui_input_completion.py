#!/usr/bin/env python3
"""Tests for the pure CompletionEngine.

The engine is UI-free, so these are plain unit tests: feed (text, cursor) and a
candidate source, assert the ranked typed candidates.
"""

from __future__ import annotations

from mewbo_cli.tui.input.completion import (
    BASH_SIGIL,
    CommandCandidate,
    CompletionEngine,
    name_priority_tier,
)

FILES = [
    "src/app.py",
    "src/widgets/app_helpers.py",
    "tests/test_app.py",
    "docs/app/overview.md",
    "README.md",
    "src/zebra/applesauce.py",
]


def _engine(files=FILES, commands=()):
    return CompletionEngine(
        files_provider=lambda: list(files),
        commands_provider=lambda: list(commands),
    )


# --- token detection ------------------------------------------------------


def test_active_token_detects_at_anchored_to_caret():
    tok = CompletionEngine.active_token("hello @app", cursor=len("hello @app"))
    assert tok is not None
    assert tok.sigil == "@"
    assert tok.query == "app"
    assert tok.start == 6


def test_active_token_slash_anywhere_in_line():
    # A "/" need not be at column 0 (slash dispatch anywhere in the line).
    text = "please run /he"
    tok = CompletionEngine.active_token(text, cursor=len(text))
    assert tok is not None
    assert tok.sigil == "/"
    assert tok.query == "he"


def test_active_token_ignores_email_at():
    assert CompletionEngine.active_token("mail bob@host", cursor=13) is None


def test_active_token_ignores_path_slash():
    # "src/app" — the "/" follows a non-space word char, so it is not a sigil.
    assert CompletionEngine.active_token("src/app", cursor=7) is None


def test_active_token_none_outside_token():
    assert CompletionEngine.active_token("plain text", cursor=5) is None


def test_active_token_spans_whole_token_for_caret_insert():
    text = "@appfile"
    tok = CompletionEngine.active_token(text, cursor=4)  # caret after "@app"
    assert tok is not None
    assert tok.query == "app"
    assert tok.end == len(text)  # token end extends past the caret


# --- @ file tiered ranking ------------------------------------------------


def test_file_exact_basename_stem_beats_prefix_and_substring():
    results = _engine().complete("@app", cursor=4)
    # "src/app.py" stem == "app" -> exact tier, must come first.
    assert results[0].display == "src/app.py"
    # all results carry the @-prefixed replacement
    assert results[0].replacement == "@src/app.py"


def test_file_basename_prefix_beats_path_segment():
    results = _engine().complete("@app", cursor=4)
    displays = [r.display for r in results]
    # app_helpers.py is a basename-prefix match (tier 1). docs/app/overview.md
    # has no basename match — "app" is an exact path SEGMENT (tier 2) — so it
    # must rank below the basename-prefix hit.
    assert displays.index("src/widgets/app_helpers.py") < displays.index(
        "docs/app/overview.md"
    )


def test_file_path_segment_match():
    # "widgets" is an exact path segment of src/widgets/app_helpers.py.
    results = _engine().complete("@widgets", cursor=8)
    assert results[0].display == "src/widgets/app_helpers.py"


def test_file_path_segment_highlights_correct_segment():
    # Regression: "app" appears both inside the first component ("xapp")
    # and as a whole second segment. The path-segment match must highlight the
    # REAL segment ("xapp/[app]/file.py" -> offsets 5..8), not the earlier
    # substring inside "x[app]".
    eng = CompletionEngine(
        files_provider=lambda: ["xapp/app/file.py"],
        commands_provider=lambda: [],
    )
    results = eng.complete("@app", cursor=4)
    assert results[0].display == "xapp/app/file.py"
    # "xapp/" is 5 chars; the matching segment "app" is at offsets 5,6,7.
    assert results[0].match_indices == (5, 6, 7)


def test_file_subsequence_fallback():
    # "tsapp" is not a substring but is a subsequence of tests/test_app.py.
    results = _engine().complete("@tsapp", cursor=6)
    assert any(r.display == "tests/test_app.py" for r in results)


def test_file_empty_query_lists_all():
    results = _engine().complete("@", cursor=1)
    assert len(results) == len(FILES)


def test_file_match_indices_point_into_display():
    results = _engine().complete("@README", cursor=7)
    top = results[0]
    assert top.display == "README.md"
    # the first 6 chars "README" are highlighted
    assert top.match_indices == tuple(range(6))


def test_name_priority_tier_public_helper():
    assert name_priority_tier("src/app.py", "app") == 0  # exact stem
    assert name_priority_tier("src/widgets/app_helpers.py", "app") == 1  # prefix
    assert name_priority_tier("docs/app/overview.md", "app") == 2  # path segment
    assert name_priority_tier("nope.py", "zzz") == 5  # no match


# --- / command fuzzy ranking ----------------------------------------------

CMDS = [
    CommandCandidate("help", "Show help", kind="command"),
    CommandCandidate("plan", "Toggle plan", kind="command", argument_hint="on|off"),
    CommandCandidate("plugins", "Manage plugins", kind="command"),
    CommandCandidate("frontend:component", "Scaffold", kind="custom", argument_hint="<name>"),
]


def test_command_exact_beats_prefix():
    results = _engine(commands=CMDS).complete("/plan", cursor=5)
    assert results[0].display == "/plan"  # exact, beats /plugins prefix


def test_command_prefix_match():
    results = _engine(commands=CMDS).complete("/pl", cursor=3)
    displays = [r.display for r in results]
    assert "/plan" in displays
    assert "/plugins" in displays


def test_command_fuzzy_subsequence():
    # "fcomp" is a subsequence of "frontend:component".
    results = _engine(commands=CMDS).complete("/fcomp", cursor=6)
    assert any(r.display == "/frontend:component" for r in results)


def test_command_argument_hint_surfaced():
    results = _engine(commands=CMDS).complete("/plan", cursor=5)
    assert results[0].argument_hint == "on|off"


def test_command_match_indices_offset_for_slash():
    results = _engine(commands=CMDS).complete("/help", cursor=5)
    top = results[0]
    # display is "/help"; matched name chars are at display offsets 1..5
    assert top.match_indices == (1, 2, 3, 4)


def test_command_empty_query_lists_all():
    results = _engine(commands=CMDS).complete("/", cursor=1)
    assert len(results) == len(CMDS)


# --- bash sigil + robustness ----------------------------------------------


def test_bash_sigil_detected_but_no_completion():
    tok = CompletionEngine.active_token("!ls -la", cursor=3)
    assert tok is not None
    assert tok.sigil == BASH_SIGIL
    assert _engine().complete("!ls", cursor=3) == []


def test_provider_error_degrades_to_empty():
    def boom():
        raise RuntimeError("nope")

    eng = CompletionEngine(files_provider=boom, commands_provider=boom)
    assert eng.complete("@a", cursor=2) == []
    assert eng.complete("/a", cursor=2) == []
