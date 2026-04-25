#!/usr/bin/env python3
"""Tests for the widget-builder component library (``examples/components/``).

Scope: the structural guarantees every component file must keep —
otherwise the copy-paste contract the sub-agent relies on silently rots.

  1. Each component lints clean under the widget's own AST import linter
     (the exact check ``submit_widget`` runs).
  2. Each file defines exactly one ``*Card`` class and that class exposes
     a ``render(self)`` method — the one public entry point.
  3. The class block sits between matching ``# ── <ClassName> ─`` /
     ``# ── end <ClassName> ─`` banners so the ``awk`` extraction idiom
     works.
  4. Every component file + class is indexed in ``components/README.md``
     so the sub-agent can discover it.

These tests are deliberately structural (file layout, symbols, README
rows), not behavioural — the component render code executes inside
stlite/Pyodide, not in this Python.  Running the functions here would
require pulling the streamlit runtime into the test env, which defeats
the point of keeping these files as stlite-only templates.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from pathlib import Path

import pytest
from truss_core.builtin_plugins.widget_builder.linter import (
    ALLOWED_MODULES,
    lint,
)


def _components_dir() -> Path:
    return Path(
        str(
            importlib.resources.files("truss_core")
            / "builtin_plugins"
            / "widget_builder"
            / "examples"
            / "components"
        )
    )


def _component_files() -> list[Path]:
    # Exclude the README and __pycache__ — only .py files that are
    # actual component implementations.
    return sorted(p for p in _components_dir().glob("*.py") if not p.name.startswith("_"))


# ---------------------------------------------------------------------------
# Fixtures & parametrization
# ---------------------------------------------------------------------------

# One test run per component — keeps failures specific ("foo.py is broken"
# not "the batch is broken") and scales naturally when new files land.
COMPONENT_FILES = _component_files()
COMPONENT_IDS = [p.name for p in COMPONENT_FILES]


@pytest.fixture(params=COMPONENT_FILES, ids=COMPONENT_IDS)
def component_file(request) -> Path:
    return request.param


# ---------------------------------------------------------------------------
# Sanity — the library is non-empty and discoverable
# ---------------------------------------------------------------------------


class TestLibraryShape:
    def test_components_dir_exists(self):
        assert _components_dir().is_dir(), (
            "components/ must exist as a sibling of finance_chart/ and data_table/"
        )

    def test_has_at_least_one_component(self):
        # A library with zero components is a dead README; fail loudly so
        # someone notices if the folder regresses to empty.
        assert COMPONENT_FILES, "no .py component files found under examples/components/"

    def test_readme_present(self):
        assert (_components_dir() / "README.md").is_file(), (
            "components/README.md is the sub-agent's entry point — it must exist"
        )


# ---------------------------------------------------------------------------
# Per-component checks — run once per file
# ---------------------------------------------------------------------------


class TestComponentFile:
    def test_lints_clean_against_widget_allowlist(self, component_file: Path):
        """Every component's copy-region must pass the widget AST linter.

        The agent extracts the block between ``# ── ClassName ─`` and
        ``# ── end ClassName ─`` (the awk idiom in each file's docstring).
        Code outside that region — the ``__main__`` preview block, any
        server-side helpers — never reaches stlite, so we don't lint it.
        Linting the whole file would force the demos to drop
        ``st.set_page_config()`` (which they need for ``streamlit run``)
        and ban tools like ``urllib`` that legitimately run server-side.
        """
        text = component_file.read_text()
        tree = ast.parse(text)
        card_name = next(
            (n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Card")),
            None,
        )
        assert card_name, f"no *Card class found in {component_file.name}"
        # Anchor at start-of-line — the docstring's awk example references
        # the banner inline (``# ── ClassName ─``) and a naive ``str.find``
        # would slice from there instead of the real banner row.
        open_re = re.compile(rf"^# ── {re.escape(card_name)} ─", re.MULTILINE)
        close_re = re.compile(rf"^# ── end {re.escape(card_name)} ─", re.MULTILINE)
        open_match = open_re.search(text)
        close_match = close_re.search(text)
        assert open_match and close_match and open_match.start() < close_match.start(), (
            f"{component_file.name} missing or malformed copy-region banners — "
            "see test_has_extraction_banners for the contract"
        )
        # End the slice at the closing banner's line break so the close
        # marker is included; the linter parses the text as a module.
        copy_region = text[open_match.start() : text.find("\n", close_match.start()) + 1]
        findings = lint(copy_region)
        assert not findings, (
            f"{component_file.name} copy-region would fail the widget AST linter:\n"
            + "\n".join(f"  - line {f.line}: {f.message} [{f.rule}]" for f in findings)
        )

    def test_exposes_card_class(self, component_file: Path):
        """Each component file must define exactly one top-level class
        whose name ends in ``Card``.  That class is the agent's copy-paste
        unit.

        We don't enforce a strict filename→PascalCase mapping because
        acronyms (``GitHubRepoCard`` vs ``github_repo_card.py``) would
        fight a naive conversion.  The end-in-``Card`` rule + the banner
        markers + the README entry are triangulation enough.
        """
        tree = ast.parse(component_file.read_text())
        card_classes = [
            n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Card")
        ]
        assert len(card_classes) == 1, (
            f"{component_file.name} must define exactly one top-level `*Card` class; "
            f"found: {card_classes}"
        )

    def test_card_class_has_render_method(self, component_file: Path):
        """The ``*Card`` class must expose a ``render(self)`` method — the
        one public entry point across the whole library."""
        tree = ast.parse(component_file.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Card"):
                methods = {m.name for m in node.body if isinstance(m, ast.FunctionDef)}
                assert "render" in methods, (
                    f"class {node.name} in {component_file.name} must define "
                    f"`render(self)`; found methods: {sorted(methods)}"
                )
                return
        pytest.fail(f"no *Card class found in {component_file.name}")

    def test_has_extraction_banners(self, component_file: Path):
        """The ``*Card`` class block must sit between matching banner
        comments so the ``awk`` extraction idiom works.

        Missing banners would force the agent to hand-pick lines, which
        bloats its context and breaks the one-command copy-paste contract.
        """
        tree = ast.parse(component_file.read_text())
        card_name = next(
            (n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Card")),
            None,
        )
        assert card_name, f"no *Card class found in {component_file.name}"
        text = component_file.read_text()
        open_marker = f"# ── {card_name} ─"
        close_marker = f"# ── end {card_name} ─"
        assert open_marker in text, f"{component_file.name} missing opening banner `{open_marker}`"
        assert close_marker in text, (
            f"{component_file.name} missing closing banner `{close_marker}`"
        )
        assert text.index(open_marker) < text.index(close_marker), (
            f"{component_file.name} banner order is inverted — opening must appear before closing"
        )

    def test_has_standalone_demo(self, component_file: Path):
        """Every component must end with an ``if __name__ == "__main__":`` demo.

        ``streamlit run <file>.py`` is how contributors preview changes
        without spinning up the full widget pipeline.  The AST checks for
        literal ``__name__ == "__main__"`` shape so a future lint-clean
        refactor can't silently delete the preview.
        """
        tree = ast.parse(component_file.read_text())
        has_demo = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators
            )
            for node in ast.walk(tree)
        )
        assert has_demo, f'{component_file.name} must end with `if __name__ == "__main__":` demo'


# ---------------------------------------------------------------------------
# README parity — component must be listed
# ---------------------------------------------------------------------------


def _readme_text() -> str:
    return (_components_dir() / "README.md").read_text(encoding="utf-8")


class TestReadmeParity:
    def test_every_component_listed_by_filename(self, component_file: Path):
        """Require each component file appear as a backtick-quoted filename
        in the README table.  Prose references rot silently; backtick keys
        fail loudly when the file is renamed."""
        assert f"`{component_file.name}`" in _readme_text(), (
            f"{component_file.name} is not indexed in components/README.md — "
            "add a row to the 'Available components' table"
        )

    def test_every_component_class_referenced(self, component_file: Path):
        """Require each component's ``*Card`` class name to appear in the
        README — so ctrl-F on the class lands the reader on the right file."""
        tree = ast.parse(component_file.read_text())
        card_name = next(
            (n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Card")),
            None,
        )
        assert card_name, f"no *Card class found in {component_file.name}"
        assert card_name in _readme_text(), (
            f"class `{card_name}` from {component_file.name} is not referenced in "
            "components/README.md"
        )


# ---------------------------------------------------------------------------
# Regression — __future__ must be permitted
# ---------------------------------------------------------------------------


class TestFutureImportAllowed:
    """``from __future__ import annotations`` is the idiomatic way to get
    PEP 563-style type hints on any Python 3.7+.  It was historically
    missing from the widget allowlist; guard against regression."""

    def test_future_in_allowlist(self):
        assert "__future__" in ALLOWED_MODULES

    def test_future_import_lints_clean(self):
        assert lint("from __future__ import annotations\nimport streamlit as st\n") == []
