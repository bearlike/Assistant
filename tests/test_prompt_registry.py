"""Unit tests for the central prompt registry mechanics (DI, in-memory entries).

Covers the contract every migration relies on: static-vs-templated rendering,
override resolution (scenario > model longest-prefix), append/prepend modes, the
programmatic modifier seam, validate_all's definition-time checks, and the
package-level ``validate_all`` smoke that gates the whole registry in CI.
"""

from __future__ import annotations

import pytest
from mewbo_core.prompt_registry import (
    PromptContext,
    PromptEntry,
    PromptOverride,
    PromptRegistry,
    get_prompt_registry,
    register_prompt_modifier,
    reset_prompt_modifiers,
)


def _registry(*entries: PromptEntry, modifiers=None) -> PromptRegistry:
    return PromptRegistry({e.id: e for e in entries}, modifiers=modifiers or [])


# ---------------------------------------------------------------------------
# Static vs templated rendering
# ---------------------------------------------------------------------------


def test_static_entry_returns_verbatim_even_with_braces():
    # A static prompt (no declared variables) must pass literal braces through
    # untouched — never Jinja-parsed.
    body = 'Return {"result": {{not a var}}} and <tag>{x}</tag>'
    reg = _registry(PromptEntry(id="s", purpose="p", template=body))
    assert reg.render("s") == body
    # Extra kwargs are ignored for a static entry.
    assert reg.render("s", anything="x") == body


def test_templated_entry_binds_declared_variables():
    reg = _registry(
        PromptEntry(
            id="t",
            purpose="p",
            template="Budget {{ used }}/{{ total }}",
            variables=["used", "total"],
        )
    )
    assert reg.render("t", used=3, total=10) == "Budget 3/10"


def test_missing_variable_is_a_render_error_not_a_silent_blank():
    reg = _registry(
        PromptEntry(id="t", purpose="p", template="Hi {{ name }}", variables=["name"])
    )
    with pytest.raises(Exception):
        reg.render("t")  # StrictUndefined


def test_unknown_id_raises_keyerror():
    reg = _registry(PromptEntry(id="s", purpose="p", template="x"))
    with pytest.raises(KeyError):
        reg.render("nope")


# ---------------------------------------------------------------------------
# Override resolution
# ---------------------------------------------------------------------------


def test_scenario_override_replaces_base():
    reg = _registry(
        PromptEntry(
            id="compact.system",
            purpose="p",
            template="FULL",
            overrides=[PromptOverride(kind="scenario", match="caveman", template="TERSE")],
        )
    )
    assert reg.render("compact.system") == "FULL"
    assert reg.render("compact.system", scenario="caveman") == "TERSE"


def test_model_override_longest_prefix_wins():
    reg = _registry(
        PromptEntry(
            id="x",
            purpose="p",
            template="BASE",
            overrides=[
                PromptOverride(kind="model", match="gem", mode="append", template="-A"),
                PromptOverride(kind="model", match="gemma-", mode="append", template="-B"),
            ],
        )
    )
    assert reg.render("x", model="gemma-2-9b") == "BASE-B"  # longest prefix
    assert reg.render("x", model="gemini-3") == "BASE-A"
    assert reg.render("x", model="opus-4-8") == "BASE"  # no match


def test_scenario_wins_over_model():
    reg = _registry(
        PromptEntry(
            id="x",
            purpose="p",
            template="BASE",
            overrides=[
                PromptOverride(kind="scenario", match="terse", template="SCEN"),
                PromptOverride(kind="model", match="gemma-", mode="append", template="-M"),
            ],
        )
    )
    assert reg.render("x", model="gemma-2", scenario="terse") == "SCEN"


def test_append_and_prepend_modes_keep_the_base_contract():
    reg = _registry(
        PromptEntry(
            id="x",
            purpose="p",
            template="CONTRACT",
            overrides=[
                PromptOverride(kind="model", match="a-", mode="append", template=" +nudge"),
                PromptOverride(kind="model", match="b-", mode="prepend", template="nudge+ "),
            ],
        )
    )
    assert reg.render("x", model="a-1") == "CONTRACT +nudge"
    assert reg.render("x", model="b-1") == "nudge+ CONTRACT"


def test_override_shares_the_entry_variable_namespace():
    reg = _registry(
        PromptEntry(
            id="x",
            purpose="p",
            template="Depth {{ d }}",
            variables=["d"],
            overrides=[
                PromptOverride(kind="model", match="g-", mode="append", template=" (cap {{ d }})")
            ],
        )
    )
    assert reg.render("x", model="g-1", d=3) == "Depth 3 (cap 3)"


# ---------------------------------------------------------------------------
# Programmatic modifier seam (mods)
# ---------------------------------------------------------------------------


class _GemmaPack:
    """A toy mod: append a compatibility footer for gemma-* models."""

    def matches(self, ctx: PromptContext) -> bool:
        return bool(ctx.model and ctx.model.startswith("gemma-"))

    def apply(self, ctx: PromptContext, rendered: str) -> str:
        return rendered + "\n[gemma footer]"


def test_modifier_applies_after_override():
    reg = _registry(
        PromptEntry(id="x", purpose="p", template="BASE"),
        modifiers=[_GemmaPack()],
    )
    assert reg.render("x", model="gemma-2") == "BASE\n[gemma footer]"
    assert reg.render("x", model="opus") == "BASE"


def test_register_prompt_modifier_is_idempotent_and_resettable():
    mod = _GemmaPack()
    try:
        register_prompt_modifier(mod)
        register_prompt_modifier(mod)
        from mewbo_core import prompt_registry as pr

        assert pr._PROMPT_MODIFIERS.count(mod) == 1
    finally:
        reset_prompt_modifiers()
        from mewbo_core import prompt_registry as pr

        assert mod not in pr._PROMPT_MODIFIERS


# ---------------------------------------------------------------------------
# Schema validation (validate at definition)
# ---------------------------------------------------------------------------


def test_entry_requires_exactly_one_source():
    with pytest.raises(Exception):
        PromptEntry(id="x", purpose="p")  # neither
    with pytest.raises(Exception):
        PromptEntry(id="x", purpose="p", template="a", template_file="b.txt")  # both


def test_entry_rejects_bad_id():
    with pytest.raises(Exception):
        PromptEntry(id="Bad ID", purpose="p", template="x")


def test_validate_all_catches_declared_variable_mismatch():
    reg = _registry(
        PromptEntry(id="x", purpose="p", template="Hi {{ name }}", variables=["nme"])
    )
    with pytest.raises(ValueError, match="declared variables"):
        reg.validate_all()


def test_validate_all_catches_undeclared_override_variable():
    reg = _registry(
        PromptEntry(
            id="x",
            purpose="p",
            template="Hi {{ name }}",
            variables=["name"],
            overrides=[
                PromptOverride(kind="model", match="g-", mode="append", template="{{ other }}")
            ],
        )
    )
    with pytest.raises(ValueError, match="undeclared"):
        reg.validate_all()


def test_duplicate_id_across_files_is_rejected(tmp_path):
    # The loader rejects an id redefined by a second YAML file in the same root.
    (tmp_path / "a.yaml").write_text("dup:\n  purpose: a\n  template: A\n")
    (tmp_path / "b.yaml").write_text("dup:\n  purpose: b\n  template: B\n")
    with pytest.raises(ValueError, match="duplicate prompt id"):
        PromptRegistry._load_dir(tmp_path, ("pkg", "prompts"), {}, {})


def test_non_mapping_yaml_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        PromptRegistry._load_dir(tmp_path, ("pkg", "prompts"), {}, {})


def test_template_file_resolves_against_owning_root():
    # An entry's template_file is read relative to its root's prompts/ dir.
    reg = get_prompt_registry()
    assert "system.txt" not in reg.render("file.system")  # content, not the path
    assert reg.render("file.action-planner").strip()


# ---------------------------------------------------------------------------
# Package-level smoke — the CI gate for every registry file
# ---------------------------------------------------------------------------


def test_package_registry_loads_and_validates():
    reg = get_prompt_registry()
    reg.validate_all()  # every entry parses, vars match, template_file targets exist
    ids = reg.list_ids()
    assert "file.system" in ids
    # The externalized files render (verbatim) and are non-empty.
    assert reg.render("file.system").strip()
    assert reg.render("file.homeassistant-set-state", ALL_ENTITIES="x")
