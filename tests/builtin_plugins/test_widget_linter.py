#!/usr/bin/env python3
"""Tests for the widget-builder AST linter pipeline.

The linter is a functional pipeline — each rule is a pure function of
``(tree, source) -> Iterable[LintFinding]``.  Tests exercise each rule in
isolation + the ``lint()`` composition + the skill/allowlist parity.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from pathlib import Path

import pytest
from truss_core.builtin_plugins.widget_builder.linter import (
    ALLOWED_MODULES,
    DEFAULT_RULES,
    LintFinding,
    check_forbidden_patterns,
    check_imports,
    format_findings,
    lint,
)

# ---------------------------------------------------------------------------
# ALLOWED_MODULES shape
# ---------------------------------------------------------------------------


class TestAllowlistShape:
    def test_is_frozenset(self):
        # Frozen so callers can't mutate it mid-run.
        assert isinstance(ALLOWED_MODULES, frozenset)

    def test_contains_streamlit(self):
        # Core invariant — without streamlit the whole plugin stops working.
        assert "streamlit" in ALLOWED_MODULES

    def test_every_entry_is_a_top_level_module(self):
        # Allowlist is keyed on top-level names; dotted paths would never match.
        for name in ALLOWED_MODULES:
            assert "." not in name, f"{name!r} has a dot"
            assert name == name.strip() and name


# ---------------------------------------------------------------------------
# check_imports — unit tests with AST fixtures
# ---------------------------------------------------------------------------


def _run(rule, source: str) -> list[LintFinding]:
    return list(rule(ast.parse(source), source))


class TestCheckForbiddenPatterns:
    def test_sidebar_attribute_flagged(self):
        findings = _run(check_forbidden_patterns, "import streamlit as st\nst.sidebar.title('x')\n")
        assert len(findings) == 1
        assert findings[0].rule == "forbidden-sidebar"
        assert findings[0].line == 2

    def test_sidebar_selectbox_flagged(self):
        code = "import streamlit as st\nst.sidebar.selectbox('y', [])\n"
        findings = _run(check_forbidden_patterns, code)
        assert any(f.rule == "forbidden-sidebar" for f in findings)

    def test_set_page_config_flagged(self):
        code = "import streamlit as st\nst.set_page_config(layout='wide')\n"
        findings = _run(check_forbidden_patterns, code)
        assert len(findings) == 1
        assert findings[0].rule == "forbidden-set-page-config"
        assert findings[0].line == 2

    def test_clean_widget_passes(self):
        src = (
            "import streamlit as st\n"
            "import json\n"
            "with open('data.json') as f:\n"
            "    d = json.load(f)\n"
            "c1, c2 = st.columns(2)\n"
            "c1.metric('Stars', d['stars'])\n"
        )
        assert _run(check_forbidden_patterns, src) == []

    def test_sidebar_in_default_rules(self):
        # Both forbidden patterns fire via the default pipeline.
        src = "import streamlit as st\nst.set_page_config()\nst.sidebar.write('x')\n"
        findings = lint(src)
        rules = {f.rule for f in findings}
        assert "forbidden-sidebar" in rules
        assert "forbidden-set-page-config" in rules


class TestCheckImports:
    def test_allowed_plain_import_passes(self):
        assert _run(check_imports, "import json\nimport streamlit as st\n") == []

    def test_allowed_from_import_passes(self):
        assert _run(check_imports, "from collections import Counter\n") == []

    def test_dotted_submodule_of_allowed_passes(self):
        # `streamlit.components.v1` is part of streamlit — top-level match wins.
        src = "from streamlit.components.v1 import html\n"
        assert _run(check_imports, src) == []

    def test_disallowed_plain_import_flagged(self):
        findings = _run(check_imports, "import torch\n")
        assert len(findings) == 1
        assert findings[0].rule == "unsupported-import"
        assert "'torch'" in findings[0].message
        assert findings[0].line == 1

    def test_disallowed_from_import_flagged(self):
        findings = _run(check_imports, "from tensorflow.keras import layers\n")
        assert len(findings) == 1
        assert findings[0].rule == "unsupported-import"
        assert "'tensorflow'" in findings[0].message

    def test_disallowed_aliased_import_flagged(self):
        findings = _run(check_imports, "import requests as r\n")
        assert len(findings) == 1
        assert "'requests'" in findings[0].message

    def test_multiple_disallowed_in_one_line_all_flagged(self):
        # `import a, b` → two alias nodes, both inspected.
        findings = _run(check_imports, "import torch, cv2\n")
        modules = sorted(re.search(r"'([^']+)'", f.message).group(1) for f in findings)
        assert modules == ["cv2", "torch"]

    def test_relative_import_ignored(self):
        # `from . import x` — node.module is None; can't reach outside the dir.
        src = "from . import helpers\n"
        assert _run(check_imports, src) == []

    def test_nested_import_inside_function_flagged(self):
        # `ast.walk` visits nested statements — catches lazy imports too.
        findings = _run(check_imports, "def f():\n    import subprocess\n")
        assert len(findings) == 1
        assert findings[0].line == 2


# ---------------------------------------------------------------------------
# lint() — pipeline composition
# ---------------------------------------------------------------------------


class TestLintPipeline:
    def test_valid_widget_returns_no_findings(self):
        src = (
            "import json\n"
            "import streamlit as st\n"
            "with open('data.json') as f:\n"
            "    data = json.load(f)\n"
            "st.write(data)\n"
        )
        assert lint(src) == []

    def test_syntax_error_short_circuits(self):
        # Broken syntax must not reach any rule.
        findings = lint("def broken(:\n")
        assert len(findings) == 1
        assert findings[0].rule == "syntax"
        assert findings[0].line > 0

    def test_default_rules_is_ordered_tuple(self):
        # The pipeline order is the public contract — a list is mutable and
        # would leak implementation.
        assert isinstance(DEFAULT_RULES, tuple)
        assert check_imports in DEFAULT_RULES

    def test_custom_rule_set_replaces_default(self):
        # Callers can swap the rule set entirely — extensibility hinge.
        empty_rules: tuple = ()
        assert lint("import torch\n", rules=empty_rules) == []

    def test_multiple_rules_findings_concatenated(self):
        def always_flag(_tree, _src):
            yield LintFinding(rule="always", message="always fires", line=1)

        findings = lint("import torch\n", rules=(check_imports, always_flag))
        rules_seen = sorted({f.rule for f in findings})
        assert rules_seen == ["always", "unsupported-import"]


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


class TestFormatFindings:
    def test_empty_produces_empty_string(self):
        assert format_findings([]) == ""

    def test_single_finding_line_shape(self):
        f = LintFinding(rule="unsupported-import", message="module 'x' …", line=3)
        assert format_findings([f]) == "- line 3: module 'x' … [unsupported-import]"

    def test_multiple_findings_newline_delimited(self):
        out = format_findings(
            [
                LintFinding("r1", "first", 1),
                LintFinding("r2", "second", 2),
            ]
        )
        assert out.count("\n") == 1


# ---------------------------------------------------------------------------
# Parity — ALLOWED_MODULES ↔ agent markdown
# ---------------------------------------------------------------------------


def _agent_md() -> str:
    root = Path(
        str(
            importlib.resources.files("truss_core")
            / "builtin_plugins"
            / "widget_builder"
            / "agents"
            / "st-widget-builder.md"
        )
    )
    return root.read_text(encoding="utf-8")


class TestAllowlistParity:
    def test_every_allowed_module_appears_in_agent_prompt(self):
        md = _agent_md()
        # Require an exact backtick-quoted mention — prose paraphrases would
        # rot without being caught.
        missing = [m for m in ALLOWED_MODULES if f"`{m}`" not in md]
        assert not missing, (
            f"Agent prompt missing allowed module(s): {sorted(missing)}. "
            "Update agents/st-widget-builder.md to keep the allowlist in sync."
        )


# ---------------------------------------------------------------------------
# End-to-end — submit_widget rejects bad imports
# ---------------------------------------------------------------------------


def test_submit_widget_rejects_unsupported_import(tmp_path, monkeypatch):
    """A widget importing `torch` is rejected before `widget_ready` fires."""
    import asyncio

    from truss_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetTool,
    )
    from truss_core.classes import ActionStep

    widget_root = tmp_path / "widgets"
    widget_dir = widget_root / "s1" / "w1"
    widget_dir.mkdir(parents=True)
    (widget_dir / "app.py").write_text("import torch\nimport streamlit as st\n")
    (widget_dir / "data.json").write_text("{}")

    monkeypatch.setenv("TRUSS_WIDGET_ROOT", str(widget_root))

    events = []
    tool = SubmitWidgetTool(session_id="s1", event_logger=events.append)

    step = ActionStep(
        tool_id="submit_widget",
        operation="run",
        tool_input={"widget_id": "w1"},
    )
    result = asyncio.run(tool.handle(step))

    assert "lint failed" in result.content
    assert "'torch'" in result.content
    assert "unsupported-import" in result.content
    # widget_ready event must NOT be emitted on lint failure.
    assert not [e for e in events if e.get("type") == "widget_ready"]


def test_submit_widget_accepts_allowed_imports_only(tmp_path, monkeypatch):
    """Widget with only allowed imports passes the lint gate."""
    import asyncio

    from truss_core.builtin_plugins.widget_builder.submit_widget import (
        SubmitWidgetTool,
    )
    from truss_core.classes import ActionStep

    widget_root = tmp_path / "widgets"
    widget_dir = widget_root / "s1" / "w1"
    widget_dir.mkdir(parents=True)
    (widget_dir / "app.py").write_text("import json\nimport streamlit as st\nst.write('hi')\n")
    (widget_dir / "data.json").write_text("{}")

    monkeypatch.setenv("TRUSS_WIDGET_ROOT", str(widget_root))

    events = []
    tool = SubmitWidgetTool(session_id="s1", event_logger=events.append)

    step = ActionStep(
        tool_id="submit_widget",
        operation="run",
        tool_input={"widget_id": "w1"},
    )
    result = asyncio.run(tool.handle(step))

    assert "lint failed" not in result.content
    assert "submitted successfully" in result.content
    assert [e for e in events if e.get("type") == "widget_ready"]


@pytest.mark.parametrize(
    "bad_src",
    [
        "import subprocess\n",
        "import socket\n",
        "from urllib.request import urlopen\n",
        "import requests\n",
        "import httpx\n",
    ],
)
def test_common_network_and_shell_imports_rejected(bad_src):
    """Sanity — the allowlist keeps out the obvious "widget shouldn't do this"
    modules.  Expands naturally as new rules are added."""
    findings = lint(bad_src)
    assert findings
    assert any(f.rule == "unsupported-import" for f in findings)
