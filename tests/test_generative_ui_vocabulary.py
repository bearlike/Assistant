#!/usr/bin/env python3
"""Tripwire: the generative-UI component vocabulary is mirrored, not shared.

The server's discriminated union (``builtin_plugins/generative_ui/nodes.py``)
and the console's renderer allowlist
(``apps/mewbo_console/src/components/generative-ui/registry.ts``) are two
hand-maintained lists of the same component names, in two languages, with no
build step between them. Each side already has its own test pinning its own
list, and that is exactly why neither can catch the failure that matters:
**divergence**.

Both directions are defects, and they fail differently enough to be worth
naming:

- A kind the server can MINT but the console does not render degrades to the
  fallback node. The conversation survives, so nothing is logged and nothing
  fails — the panel is just quietly wrong.
- A kind the console renders but the server cannot mint is dead code that
  reads as a supported feature to the next person who greps for it.

The model-facing skill is checked against the same source, because a skill
naming a component that does not exist teaches a model to make a call that
can only fail validation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from mewbo_core.builtin_plugins.generative_ui.nodes import (
    GenerativeUINodeUnion,
    GenerativeUISpec,
)
from mewbo_core.builtin_plugins.generative_ui.present_ui import PresentUiArgs

REPO_ROOT = Path(__file__).resolve().parents[1]

CONSOLE_REGISTRY_TS = (
    REPO_ROOT / "apps/mewbo_console/src/components/generative-ui/registry.ts"
)
GENERATIVE_UI_SKILL = (
    REPO_ROOT
    / "packages/mewbo_core/src/mewbo_core/builtin_plugins/generative_ui"
    / "skills/generative-ui/SKILL.md"
)


def _server_components() -> set[str]:
    """The component names the server can mint, read off the union itself.

    Derived from ``GenerativeUINodeUnion`` rather than a literal list here, so
    this test cannot drift from the parse seam it is meant to guard — adding a
    twelfth variant updates this side for free and fails only the mirrors that
    were genuinely not updated.
    """
    names: set[str] = set()
    for member in _union_members():
        # The discriminator is a one-member ``Literal`` on each variant.
        annotation = member.model_fields["component"].annotation
        names.update(get_args(annotation))
    assert names, (
        "Parsed no component names off GenerativeUINodeUnion. The union's "
        "shape changed — fix this parser rather than deleting the assertion, "
        "which is the only thing comparing the two languages."
    )
    return names


def _union_members() -> tuple[type, ...]:
    """Unwrap ``Annotated[A | B | ..., Field(discriminator=...)]`` to its arms.

    ``get_args`` on the ``Annotated`` alias yields ``(inner_union, metadata)``;
    a second ``get_args`` on the inner union yields the variants. Both the
    ``typing.Union`` and the ``A | B`` (``types.UnionType``) spellings answer
    ``get_args`` identically, so no branch on which one the source used.
    """
    inner = get_args(GenerativeUINodeUnion)[0]
    return get_args(inner)


def _console_components() -> set[str]:
    """The component names the console will render, parsed from the allowlist."""
    source = CONSOLE_REGISTRY_TS.read_text(encoding="utf-8")
    block = re.search(
        r"GENERATIVE_UI_COMPONENTS\s*:\s*GenerativeUIComponentRegistry\s*=\s*\{(.*?)\}",
        source,
        re.DOTALL,
    )
    assert block is not None, (
        f"Could not find the GENERATIVE_UI_COMPONENTS literal in "
        f"{CONSOLE_REGISTRY_TS.relative_to(REPO_ROOT)}. If the allowlist moved "
        f"or changed shape, update this parser — do not delete the assertion, "
        f"which is the only thing comparing the two languages."
    )
    return set(re.findall(r"^\s*(\w+)\s*:", block.group(1), re.MULTILINE))


def test_console_renders_every_component_the_server_can_mint() -> None:
    """A mintable kind with no renderer degrades to the fallback, silently."""
    missing = _server_components() - _console_components()
    assert not missing, (
        f"{sorted(missing)} can be minted by the server union but "
        f"{CONSOLE_REGISTRY_TS.relative_to(REPO_ROOT)} does not render them. "
        f"A panel using one degrades to the fallback node — the conversation "
        f"survives, so nothing fails and nothing is logged. Add the renderer, "
        f"or drop the variant from the union."
    )


def test_server_can_mint_every_component_the_console_renders() -> None:
    """A renderer for a kind nothing mints is dead code that reads as a feature."""
    extra = _console_components() - _server_components()
    assert not extra, (
        f"{sorted(extra)} are registered as renderers in "
        f"{CONSOLE_REGISTRY_TS.relative_to(REPO_ROOT)} but no variant of the "
        f"server union mints them. Either add the variant or delete the "
        f"renderer; leaving it reads as a supported component to the next "
        f"person who greps for it."
    )


@pytest.mark.skipif(
    not GENERATIVE_UI_SKILL.exists(),
    reason="the generative-ui skill has not landed yet",
)
def test_skill_does_not_restate_the_component_vocabulary() -> None:
    """The skill must DEFER to the derived guide, never mirror it.

    This assertion is deliberately the inverse of the one it replaced. The skill
    used to carry its own component table, pinned against the union by a test —
    so it could not drift, but it was still a second copy, and a guarded mirror
    is still a mirror: it doubled the skill's length, restated what the bound
    schema already says, and had to be edited in lockstep with the union for no
    gain. ``component_guide()`` is derived from the same models that validate
    the call and is appended to the tool description, so it reaches the model
    first and cannot be stale.

    It is also what the truncation defect ate. With ``activate_skill`` capped at
    2000 characters, the skill body reached the model cut mid-table between the
    ``Divider`` and ``Link`` rows — the mirror's own bulk is what pushed the
    parts only IT carried (when to reach for a panel, the worked call) past the
    cut.

    A row naming a component and its fields is the shape to catch. Prose that
    mentions one in passing is not, which is why this looks for the TABLE.
    """
    text = GENERATIVE_UI_SKILL.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`([A-Za-z]+)`\s*\|", text, re.MULTILINE)
    restated = set(rows) & _server_components()
    assert not restated, (
        f"{GENERATIVE_UI_SKILL.relative_to(REPO_ROOT)} has re-grown a component "
        f"table naming {sorted(restated)}. The vocabulary has ONE model-facing "
        f"home — GenerativeUISpec.component_guide(), appended to the present_ui "
        f"description and to every rejection. A copy here is a second source of "
        f"truth that costs skill budget and buys nothing the schema does not "
        f"already state."
    )


@pytest.mark.skipif(
    not GENERATIVE_UI_SKILL.exists(),
    reason="the generative-ui skill has not landed yet",
)
def test_skill_examples_validate_as_whole_tool_calls() -> None:
    """Every worked example in the skill must be a call the tool would accept.

    Validated against ``PresentUiArgs`` — the WHOLE argument object — and not
    against the tree alone, which is the specific gap this replaced. The old
    example was a bare ``{"root": [...]}`` fragment with nothing saying where it
    belonged, and a traced model that had read it still had to guess the
    envelope. An example that validates as a fragment but not as a call teaches
    a call that can only fail.

    An example carrying an invented optional field, or a container nested where
    the model forbids it, is the same class of defect — and nothing else in the
    suite would notice, because a skill is just prose to every other test.
    """
    blocks = re.findall(
        r"```json\n(.*?)```", GENERATIVE_UI_SKILL.read_text(encoding="utf-8"), re.DOTALL
    )
    assert blocks, (
        f"{GENERATIVE_UI_SKILL.relative_to(REPO_ROOT)} carries no ```json "
        f"example. The example is the part of this skill that teaches the call "
        f"shape; without one this test passes while guarding nothing."
    )
    for index, block in enumerate(blocks):
        args = PresentUiArgs.model_validate(json.loads(block))
        # Degradation is the contract for every non-visual client, so exercise
        # it here too — a node that renders but cannot describe itself reaches
        # the CLI and MCP surfaces as a blank.
        assert args.to_text().strip(), (
            f"Example {index} in {GENERATIVE_UI_SKILL.relative_to(REPO_ROOT)} "
            f"validates but produces empty alt-text, so it would reach a "
            f"non-visual client as a blank panel."
        )


@pytest.mark.skipif(
    not GENERATIVE_UI_SKILL.exists(),
    reason="the generative-ui skill has not landed yet",
)
def test_the_component_guide_covers_every_variant_and_its_required_fields() -> None:
    """The ONE model-facing vocabulary names every component and every required field.

    ``component_guide()`` is what reaches the model in the tool description and
    in every rejection — the two places a model actually reads. Now that the
    skill defers to it rather than mirroring it, this is the whole of that
    contract, so it checks the required FIELDS too and not just the names: a
    component listed with the wrong required field is worse than a missing one,
    because it reads as authoritative and produces a call that fails validation
    every time.

    It is derived, so it cannot fail by omission today. It fails the day someone
    replaces the derivation with a hand-written string, which is exactly the
    regression worth catching — and the one that would now be invisible, since
    there is no second copy left to disagree with it.
    """
    guide = GenerativeUISpec.component_guide()
    for member in _union_members():
        tag = member.component_tag()
        line = next(
            (row for row in guide.splitlines() if row.startswith(f"- {tag}:")), None
        )
        assert line is not None, (
            f"{tag} is a mintable component but does not appear in "
            f"component_guide(), which is the only vocabulary a model gets "
            f"without dereferencing $ref."
        )
        required = {
            field
            for field, info in member.model_fields.items()
            if field != "component" and info.is_required()
        }
        # The guide states required fields before any "(optional: …)" clause, so
        # the head of the line is what must name them.
        head = line.split("(optional:")[0]
        missing = {field for field in required if field not in head}
        assert not missing, (
            f"component_guide() lists {tag} without its required field(s) "
            f"{sorted(missing)}. A model reading it would compose a call that "
            f"cannot validate."
        )


def test_the_guide_states_the_shape_of_every_structured_field() -> None:
    """A field's SHAPE travels in the guide, derived, wherever it is structured.

    The three dominant tree-level rejections — a child missing its
    ``component``, a list-of-lists where list-of-objects is declared, an
    invented key — are all a model getting a field's shape slightly wrong,
    and the shapes lived only behind the ``$ref`` hop the guide exists to
    remove. Asserted DERIVED (via the same ``_shape_of`` the guide renders
    with) so a new structured field cannot ship shapeless, plus the three
    decided literals verbatim so the derivation itself cannot silently
    degrade into something a model can no longer read.
    """
    guide = GenerativeUISpec.component_guide()
    for member in _union_members():
        tag = member.component_tag()
        line = next(row for row in guide.splitlines() if row.startswith(f"- {tag}:"))
        for name, info in member.model_fields.items():
            if name == "component":
                continue
            shape = GenerativeUISpec._shape_of(info.annotation)
            if shape is not None:
                assert f"{name}: {shape}" in line, (
                    f"component_guide() lists {tag}.{name} without its shape "
                    f"{shape!r} — the exact omission behind the dominant "
                    f"rejection families."
                )
    assert "items: [{label, value}]" in guide
    assert "columns: [str]" in guide
    assert "rows: [[str]], each row exactly as long as `columns`" in guide
    assert "children: [node, ...], every child carries its own `component`" in guide
