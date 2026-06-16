"""Unit tests for the controllable model→tool-variant map (Gitea #113).

Covers the contract the edit-tool selector relies on: longest-prefix matching,
the conservative default, the shipped data file loading + validating in CI, the
migrated built-in defaults being byte-for-byte preserved through
``model_prefers_structured_patch``, and the file genuinely OVERRIDING code.
"""

from __future__ import annotations

import pytest
from mewbo_core.model_variants import (
    ModelProfile,
    ModelVariantDefaults,
    ModelVariantMap,
    ModelVariantRegistry,
    get_model_variant_registry,
    reset_model_variant_registry,
)


def _registry(
    *profiles: ModelProfile, default: str = "search_replace_block"
) -> ModelVariantRegistry:
    return ModelVariantRegistry(
        ModelVariantMap(
            defaults=ModelVariantDefaults(edit_tool=default),  # type: ignore[arg-type]
            profiles=list(profiles),
        )
    )


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------


def test_longest_prefix_wins():
    reg = _registry(
        ModelProfile(match="gpt", edit_tool="search_replace_block"),
        ModelProfile(match="gpt-5", edit_tool="structured_patch"),
    )
    assert reg.edit_tool_for("gpt-5-mini") == "structured_patch"  # longer prefix
    assert reg.edit_tool_for("gpt-3.5") == "search_replace_block"  # only "gpt" matches


def test_provider_prefix_is_stripped():
    reg = _registry(ModelProfile(match="gpt-5", edit_tool="structured_patch"))
    assert reg.edit_tool_for("openai/gpt-5") == "structured_patch"
    assert reg.edit_tool_for("GPT-5") == "structured_patch"  # case-insensitive


def test_no_match_returns_default():
    reg = _registry(ModelProfile(match="gpt-5", edit_tool="structured_patch"))
    assert reg.edit_tool_for("claude-opus-4-8") == "search_replace_block"
    assert reg.edit_tool_for(None) == "search_replace_block"
    assert reg.edit_tool_for("") == "search_replace_block"


def test_profile_for_returns_the_matched_entry():
    p = ModelProfile(match="o3", edit_tool="structured_patch", note="reasoning")
    reg = _registry(p)
    assert reg.profile_for("o3-pro") is p
    assert reg.profile_for("gemini-3") is None


# ---------------------------------------------------------------------------
# Schema validation (validate at definition)
# ---------------------------------------------------------------------------


def test_unknown_edit_tool_variant_is_rejected():
    with pytest.raises(Exception):
        ModelProfile(match="x", edit_tool="not_a_tool")  # type: ignore[arg-type]


def test_extra_keys_are_forbidden():
    with pytest.raises(Exception):
        ModelProfile(match="x", edit_tool="structured_patch", bogus=1)  # type: ignore[call-arg]


def test_validate_all_catches_duplicate_match():
    reg = _registry(
        ModelProfile(match="gpt-5", edit_tool="structured_patch"),
        ModelProfile(match="gpt-5", edit_tool="search_replace_block"),
    )
    with pytest.raises(ValueError, match="duplicate profile match"):
        reg.validate_all()


def test_bad_file_degrades_to_conservative_default(monkeypatch):
    # A broken document must not crash the loop — empty map, conservative default.
    import mewbo_core.model_variants as mv

    def _boom(*_a, **_k):
        raise RuntimeError("corrupt yaml")

    monkeypatch.setattr(mv.yaml, "safe_load", _boom)
    reg = ModelVariantRegistry.from_package()
    assert reg.edit_tool_for("gpt-5") == "search_replace_block"


# ---------------------------------------------------------------------------
# Shipped data file — the CI gate + migrated-default preservation
# ---------------------------------------------------------------------------


def test_shipped_file_loads_and_validates():
    reset_model_variant_registry()
    reg = get_model_variant_registry()
    reg.validate_all()  # no duplicate matches, all entries well-formed
    reset_model_variant_registry()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-5-mini", "structured_patch"),
        ("gpt-4o", "structured_patch"),
        ("o3", "structured_patch"),
        ("o4-mini", "structured_patch"),
        ("codex-latest", "structured_patch"),
        ("claude-opus-4-8", "search_replace_block"),
        ("gemini-3-flash", "search_replace_block"),
        ("gemma-2-9b", "search_replace_block"),
    ],
)
def test_shipped_defaults_match_legacy_builtin(model, expected):
    # These are the exact verdicts the old in-code list produced — the migration
    # to a data file must be behaviour-preserving.
    reset_model_variant_registry()
    assert get_model_variant_registry().edit_tool_for(model) == expected
    reset_model_variant_registry()


def test_model_prefers_structured_patch_reads_the_file():
    # The public selector now routes through the data file for its defaults.
    from mewbo_core.llm import model_prefers_structured_patch

    reset_model_variant_registry()
    assert model_prefers_structured_patch("openai/gpt-5") is True
    assert model_prefers_structured_patch("claude-opus-4-8") is False
    assert model_prefers_structured_patch(None) is False
    reset_model_variant_registry()


def test_in_memory_map_overrides_a_models_variant():
    # Demonstrates controllability: flipping a model's variant is a data edit.
    reg = _registry(ModelProfile(match="claude", edit_tool="structured_patch"))
    assert reg.edit_tool_for("claude-opus-4-8") == "structured_patch"
