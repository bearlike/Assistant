"""Per-model prompt variant tests (Gitea #113, Phase A).

Drives the real ``get_prompt_registry()`` to prove the first model-prefix
overrides converge rather than fork: a gemma-class model and a structured-patch
(gpt-) model each render the SHARED base contract PLUS a small declared delta,
while a non-divergent model renders byte-identical to base. These are
contract/convergence tests, not namesake tests — they assert the base contract
survives the append (the whole point of `mode: append`).
"""

from __future__ import annotations

from mewbo_core.prompt_registry import get_prompt_registry

# Markers from the shared base contract in `loop.depth.root` (must survive append).
_BASE_DIRECT_EXECUTION = "## Default: Direct execution"
_BASE_WHEN_TO_STOP = "## When to stop"

# The gemma behavioural nudge marker (Override 1, loop.yaml).
_GEMMA_NUDGE = "# Compatibility nudge (gemma)"

# The structured_patch discipline nudge marker (Override 2, files.yaml).
_STRUCTURED_PATCH_NUDGE = "Structured-patch discipline"

# A model matching no override prefix — proves the base render is untouched.
_NON_DIVERGENT_MODEL = "claude-opus-4-8"


def test_base_root_has_contract_but_no_gemma_nudge():
    # No model → base render: the contract markers are present, the gemma
    # behavioural nudge is NOT.
    reg = get_prompt_registry()
    base = reg.render("loop.depth.root", plan_mode=False, depth=0, max_depth=3)
    assert _BASE_DIRECT_EXECUTION in base
    assert _BASE_WHEN_TO_STOP in base
    assert _GEMMA_NUDGE not in base


def test_gemma_override_appends_nudge_and_preserves_contract():
    # A gemma-class model gets the FULL base contract (convergence — append did
    # not replace it) PLUS its behavioural nudge.
    reg = get_prompt_registry()
    rendered = reg.render(
        "loop.depth.root", model="gemma-2-9b", plan_mode=False, depth=0, max_depth=3
    )
    assert _BASE_DIRECT_EXECUTION in rendered
    assert _BASE_WHEN_TO_STOP in rendered
    assert _GEMMA_NUDGE in rendered


def test_non_divergent_model_renders_byte_identical_to_base():
    # A model matching no override prefix renders exactly the base — no override.
    reg = get_prompt_registry()
    base = reg.render("loop.depth.root", plan_mode=False, depth=0, max_depth=3)
    with_model = reg.render(
        "loop.depth.root",
        model=_NON_DIVERGENT_MODEL,
        plan_mode=False,
        depth=0,
        max_depth=3,
    )
    assert with_model == base
    assert _GEMMA_NUDGE not in with_model


def test_file_edit_structured_patch_nudge_paired_to_gpt_prefix():
    # The structured_patch (gpt-) edit-tool guidance: base file-edit text PLUS
    # the discipline nudge; base (no model) does not contain the nudge.
    reg = get_prompt_registry()
    base = reg.render("file.tools.file-edit")
    assert "exact string replacement" in base
    assert _STRUCTURED_PATCH_NUDGE not in base

    gpt = reg.render("file.tools.file-edit", model="gpt-5-mini")
    assert "exact string replacement" in gpt  # base guidance preserved (append)
    assert _STRUCTURED_PATCH_NUDGE in gpt


def test_registry_validate_all_passes():
    # The CI gate: every entry + override parses and its declared/used vars agree.
    get_prompt_registry().validate_all()
