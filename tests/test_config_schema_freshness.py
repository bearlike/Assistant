"""The committed config artifacts match the model they are generated from.

``configs/app.schema.json`` and ``docs/configuration.md`` are both COMMITTED and
both GENERATED — the schema from ``AppConfig`` by
``scripts/ci/generate_config_schema.py``, the reference page from the schema by
``docs/hooks/schema_to_md.py``. Adding a field to ``config.py`` and not
regenerating leaves them describing a model that no longer exists, and the
symptom is silent: the console's Settings pane reads the schema, so a knob that
never reached it simply is not there, with nothing raised and nothing logged.

**This test is what checks it.** CI does run — the coverage workflow invokes a
bare ``pytest``, which honours ``testpaths`` and so collects this file — but the
workflow only re-runs the check; it does not perform one. A generated artifact
with no test behind it therefore has nothing checking it, no matter how much CI
is configured. The gating workflows are also ``pull_request``-only, so a plain
push to a branch runs nothing at all: open the PR to get the signal.

Neither test invokes a writing path. The schema half imports the generator's
pure builder and compares its return value; the docs half only READS the two
committed files. A test that regenerated first would have written to the repo as
a side effect and could "pass" by overwriting the very drift it exists to
report.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "ci" / "generate_config_schema.py"
RENDERER_PATH = REPO_ROOT / "docs" / "hooks" / "schema_to_md.py"
SCHEMA_PATH = REPO_ROOT / "configs" / "app.schema.json"
DOCS_PATH = REPO_ROOT / "docs" / "configuration.md"

_REGENERATE_SCHEMA = "uv run python scripts/ci/generate_config_schema.py"
# The docs page is written by an MkDocs ``on_pre_build`` hook, so a full docs
# build regenerates it — this is the same hook invoked directly, which is
# cheaper and is what a developer wants when only this file is stale.
_REGENERATE_DOCS = (
    "uv run python -c "
    "'import docs.hooks.schema_to_md as h; h.on_pre_build()'"
)


def _load_by_path(name: str, path: Path):
    """Import a build script by path — neither ``scripts/`` nor ``docs/`` is a package.

    Both module bodies define path constants and functions only; the writing
    entry points (``main`` under ``__main__``, ``on_pre_build`` called by
    MkDocs) never run at import, so this writes nothing.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator():
    return _load_by_path("generate_config_schema", GENERATOR_PATH)


def _load_renderer():
    return _load_by_path("schema_to_md", RENDERER_PATH)


class TestTheCommittedSchemaMatchesTheModel:
    """``configs/app.schema.json`` is what ``AppConfig`` would generate today."""

    def test_the_generator_still_exposes_a_pure_builder(self):
        # The whole test rests on being able to BUILD the schema without
        # writing it. If this fails, `main()` has absorbed the builder and the
        # fix is to extract it again — never to shell out to the writing path.
        module = _load_generator()
        assert callable(module._generate_schema)
        assert module.SCHEMA_OUTPUT_PATH == SCHEMA_PATH, (
            "the generator writes somewhere other than the file this test reads"
        )

    def test_the_committed_schema_is_current(self):
        module = _load_generator()
        # Parsed, never bytes: key order and a trailing newline are not drift,
        # and a test that failed on them would get weakened rather than fixed.
        expected = json.loads(module._generate_schema())
        committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert committed == expected, (
            "configs/app.schema.json is STALE — it no longer matches AppConfig in "
            "packages/mewbo_core/src/mewbo_core/config.py.\n"
            f"Regenerate it with:  {_REGENERATE_SCHEMA}\n"
            "The console's Settings pane reads this file, so a knob missing here is "
            "a knob no operator can set. (A diff of the two JSON documents is "
            "omitted deliberately: it is unreadable and the fix is the command above.)"
        )

    def test_appconfig_still_exists_where_the_generator_looks_for_it(self):
        # The generator's own AST guard, run as a test rather than only at
        # generation time — a rename of AppConfig otherwise surfaces as a
        # traceback from a script nobody runs.
        _load_generator()._ast_check()


class TestTheCommittedReferencePageMatchesTheSchema:
    """``docs/configuration.md`` is committed too, so it CAN go stale in the tree.

    It is generated at docs-BUILD time rather than in-commit, which is exactly
    why it drifts: regenerating the schema does NOT touch it, so a field added
    to ``config.py`` reaches the schema in the same commit and the published
    reference only whenever someone next builds the docs.

    Asserted against the renderer's own output, never against a second
    traversal of the schema written here — the page does not render every leaf
    the schema declares (a nested-of-nested model resolves to a stub), so a
    hand-rolled "every key appears" check both over-reports and is a second
    implementation of the rule, free to drift from the real one.
    """

    def test_the_committed_page_is_current(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        expected = _load_renderer().render_markdown(schema)
        assert DOCS_PATH.read_text(encoding="utf-8") == expected, (
            "docs/configuration.md is STALE — it is a COMMITTED generated file and no "
            "longer matches configs/app.schema.json.\n"
            f"Regenerate it with:  {_REGENERATE_DOCS}\n"
            "(Run from the repo root — the hook resolves its paths relative to CWD.) "
            "If the schema is stale too, regenerate that FIRST; this page is built "
            "from it, not from config.py."
        )

    def test_the_renderer_still_exposes_a_pure_builder(self):
        # Same premise as the schema half: build without writing. If this fails,
        # `render_markdown` has been folded back into `on_pre_build` and the fix
        # is to extract it again — never to call the writing hook from a test.
        assert callable(_load_renderer().render_markdown)

    def test_the_page_is_still_the_generated_one(self):
        # The control: equality proves nothing about staleness if the file were
        # hand-authored, so pin the marker the renderer writes. The marker sits
        # a few lines into the masthead, not on line 1 — the theme only lifts a
        # leading H1 into the page title when it is the file's literal first
        # block, so the renderer emits the H1 before the marker on purpose.
        # Check the masthead block rather than assuming a fixed line index.
        head_block = "\n".join(DOCS_PATH.read_text(encoding="utf-8").lstrip().splitlines()[:6])
        assert "AUTO-GENERATED from configs/app.schema.json" in head_block


def test_the_two_artifacts_are_actually_tracked():
    """Both files are in git, which is the premise of both suites above.

    If either stopped being committed the freshness question would be moot and
    these tests would be asserting against something a clean checkout lacks.
    """
    for path in (SCHEMA_PATH, DOCS_PATH):
        if not path.exists():
            pytest.fail(f"{path} is missing — it is a committed artifact")
