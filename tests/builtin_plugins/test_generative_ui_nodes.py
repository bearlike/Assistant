"""The generative-UI node vocabulary — schema, degradation, and every refusal.

These tests drive the real models: the union is the trust boundary between an
LLM's output and every client that renders it, so each rejection path is
exercised through ``model_validate`` the way the tool reaches it, not through a
helper that could pass while the production path fails.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest
from mewbo_core.builtin_plugins.generative_ui.nodes import (
    CODE_MAX_CHARS,
    LABEL_MAX_CHARS,
    MAX_SPEC_CHARS,
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    PROSE_MAX_CHARS,
    GenerativeUINode,
    GenerativeUINodeUnion,
    GenerativeUISpec,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _union_members() -> tuple[type[GenerativeUINode], ...]:
    """The eleven concrete variants behind the Annotated discriminated union."""
    inner = typing.get_args(GenerativeUINodeUnion)[0]
    return typing.get_args(inner)


# A tree touching every one of the eleven components, used as the golden
# input for both projections. Kept as raw dicts so it exercises the same parse
# seam the model's tool arguments take.
REPRESENTATIVE_TREE: dict[str, object] = {
    "root": [
        {"component": "Heading", "value": "Deployment report", "level": 1},
        {
            "component": "Card",
            "title": "Pipeline",
            "children": [
                {
                    "component": "Stack",
                    "direction": "horizontal",
                    "gap": "sm",
                    "children": [
                        {"component": "Badge", "label": "passing", "status": "success"},
                        {"component": "Badge", "label": "slow", "status": "warning"},
                    ],
                },
                {
                    "component": "Table",
                    "columns": ["job", "duration"],
                    "rows": [["lint", "12s"], ["test", "4m"]],
                },
            ],
        },
        {
            "component": "KeyValue",
            "items": [
                {"label": "commit", "value": "a1b2c3d"},
                {"label": "branch", "value": "main"},
            ],
        },
        {"component": "Text", "value": "Cache was cold on this run.", "tone": "muted"},
        {"component": "CodeBlock", "code": "def build():\n    return 0", "language": "python"},
        {
            "component": "Alert",
            "title": "Heads up",
            "body": "Retry budget spent.",
            "variant": "warning",
        },
        {"component": "Divider"},
        {"component": "Link", "href": "https://example.com/runs/9", "label": "full log"},
    ]
}


# ---------------------------------------------------------------------------
# Structural invariants — what must hold for EVERY variant, forever
# ---------------------------------------------------------------------------


class TestUnionStructure:
    def test_union_holds_exactly_the_eleven_v1_components(self):
        names = sorted(m.model_fields["component"].default for m in _union_members())
        assert names == [
            "Alert",
            "Badge",
            "Card",
            "CodeBlock",
            "Divider",
            "Heading",
            "KeyValue",
            "Link",
            "Stack",
            "Table",
            "Text",
        ]

    @pytest.mark.parametrize("member", _union_members(), ids=lambda m: m.__name__)
    def test_every_variant_owns_its_own_to_text(self, member):
        """A variant inheriting the base's ``to_text`` raises at render time.

        Caught exactly this way during development: ``LinkNode`` shipped its
        href validator but no ``to_text``, and the failure only surfaced when a
        tree containing a link was degraded — i.e. on the surface (CLI, mobile,
        Home Assistant) least likely to be exercised first.
        """
        assert member.to_text is not GenerativeUINode.to_text

    @pytest.mark.parametrize("member", _union_members(), ids=lambda m: m.__name__)
    def test_every_variant_forbids_extra_keys(self, member):
        assert member.model_config["extra"] == "forbid"

    @pytest.mark.parametrize("member", _union_members(), ids=lambda m: m.__name__)
    def test_only_containers_declare_children(self, member):
        """``children`` exists on Card and Stack alone — the leaf guard is structural."""
        has_children = "children" in member.model_fields
        assert has_children == (member.model_fields["component"].default in {"Card", "Stack"})


# ---------------------------------------------------------------------------
# Golden: the model-facing JSON Schema
# ---------------------------------------------------------------------------


class TestToolSchemaGolden:
    """The schema is what the model sees; a silent change to it is a silent
    behaviour change on every session. Pinned so the diff is the review."""

    def test_schema_matches_the_pinned_fixture(self):
        from mewbo_core.builtin_plugins.generative_ui.present_ui import PRESENT_UI_SCHEMA

        expected = json.loads((FIXTURES / "generative_ui_tool_schema.json").read_text())
        # Round-trip through the same canonicalisation the fixture was written
        # with, so key ORDER is not what this test is about.
        assert json.loads(json.dumps(PRESENT_UI_SCHEMA, sort_keys=True)) == expected

    def test_schema_carries_the_component_discriminator(self):
        """``root`` is a TOP-LEVEL argument — there is no ``spec`` wrapper.

        The wrapper was the single most-failed part of this tool across two
        traced sessions, so its absence here is the assertion, not an incidental
        path change: reading through a ``$defs/GenerativeUISpec`` hop again
        would mean it had come back.
        """
        from mewbo_core.builtin_plugins.generative_ui.present_ui import PRESENT_UI_SCHEMA

        params = PRESENT_UI_SCHEMA["function"]["parameters"]
        assert sorted(params["required"]) == ["root", "summary"]
        assert "spec" not in params["properties"]
        items = params["properties"]["root"]["items"]
        assert items["discriminator"]["propertyName"] == "component"
        assert len(items["discriminator"]["mapping"]) == 11
        assert len(items["oneOf"]) == 11

    def test_defs_are_keyed_by_component_tag_not_python_class_name(self):
        """A leaked ``$defs`` name must be a name that validates.

        Pydantic keys a definition after its CLASS, so this schema used to offer
        ``#/$defs/AlertNode`` for a component whose only legal tag is ``Alert``.
        A traced model copied that key into a payload — twice — after failing to
        dereference the ``$ref`` it belonged to. Every pointer is checked, not
        just the keys: a rename that missed the discriminator mapping would
        leave the schema self-inconsistent, which is worse than not renaming.
        """
        from mewbo_core.builtin_plugins.generative_ui.present_ui import PRESENT_UI_SCHEMA

        params = PRESENT_UI_SCHEMA["function"]["parameters"]
        tags = {member.component_tag() for member in _union_members()}
        assert tags <= set(params["$defs"])
        assert not [name for name in params["$defs"] if name.endswith("Node")]
        mapping = params["properties"]["root"]["items"]["discriminator"]["mapping"]
        assert mapping == {tag: f"#/$defs/{tag}" for tag in sorted(tags)}
        # The recursive arm too — ``Card.children`` re-references the union.
        child_map = params["$defs"]["Card"]["properties"]["children"]["items"][
            "discriminator"
        ]["mapping"]
        assert child_map == mapping

    def test_schema_never_offers_a_reconciliation_key(self):
        """``key`` is emitted by nobody and offered to the model nowhere.

        It is a sixth of the schema's bytes, bound on every LLM call, for a
        React hint no v1 component can benefit from (none hold state).
        """
        from mewbo_core.builtin_plugins.generative_ui.present_ui import PRESENT_UI_SCHEMA

        defs = PRESENT_UI_SCHEMA["function"]["parameters"]["$defs"]
        for name, node_schema in defs.items():
            assert "key" not in node_schema.get("properties", {}), name


# ---------------------------------------------------------------------------
# Golden: the two projections
# ---------------------------------------------------------------------------


class TestProjectionsGolden:
    def test_to_wire_matches_the_frozen_renderer_shape(self):
        spec = GenerativeUISpec.model_validate(REPRESENTATIVE_TREE)
        assert spec.to_wire() == {
            "root": [
                {"component": "Heading", "props": {"value": "Deployment report", "level": 1}},
                {
                    "component": "Card",
                    "props": {"title": "Pipeline"},
                    "children": [
                        {
                            "component": "Stack",
                            "props": {"direction": "horizontal", "gap": "sm"},
                            "children": [
                                {
                                    "component": "Badge",
                                    "props": {"label": "passing", "status": "success"},
                                },
                                {
                                    "component": "Badge",
                                    "props": {"label": "slow", "status": "warning"},
                                },
                            ],
                        },
                        {
                            "component": "Table",
                            "props": {
                                "columns": ["job", "duration"],
                                "rows": [["lint", "12s"], ["test", "4m"]],
                            },
                        },
                    ],
                },
                {
                    "component": "KeyValue",
                    "props": {
                        "items": [
                            {"label": "commit", "value": "a1b2c3d"},
                            {"label": "branch", "value": "main"},
                        ]
                    },
                },
                {
                    "component": "Text",
                    "props": {"value": "Cache was cold on this run.", "tone": "muted"},
                },
                {
                    "component": "CodeBlock",
                    "props": {"code": "def build():\n    return 0", "language": "python"},
                },
                {
                    "component": "Alert",
                    "props": {
                        "body": "Retry budget spent.",
                        "title": "Heads up",
                        "variant": "warning",
                    },
                },
                {"component": "Divider", "props": {}},
                {
                    "component": "Link",
                    "props": {"href": "https://example.com/runs/9", "label": "full log"},
                },
            ]
        }

    def test_to_text_degradation_is_byte_pinned(self):
        """What the CLI, mobile and Home Assistant actually render."""
        spec = GenerativeUISpec.model_validate(REPRESENTATIVE_TREE)
        assert spec.to_text() == (
            "## Deployment report\n"
            "Pipeline\n"
            "  [passing]\n"
            "  [slow]\n"
            "  | job | duration |\n"
            "  | --- | --- |\n"
            "  | lint | 12s |\n"
            "  | test | 4m |\n"
            "commit: a1b2c3d\n"
            "branch: main\n"
            "Cache was cold on this run.\n"
            "```python\n"
            "def build():\n"
            "    return 0\n"
            "```\n"
            "WARNING: Heads up — Retry budget spent.\n"
            "---\n"
            "full log (https://example.com/runs/9)"
        )

    def test_props_are_the_typed_fields_with_structure_removed(self):
        """The conversion is total: no field is dropped and none is invented.

        A container's ``id`` counts as structure — it addresses the node for
        append/update server-side and is deliberately kept off the wire so the
        frozen renderer shape is byte-identical with or without addressing.
        """
        spec = GenerativeUISpec.model_validate(REPRESENTATIVE_TREE)
        card = spec.root[1]
        wire = card.to_spec_node()
        assert set(wire) == {"component", "props", "children"}
        assert set(wire["props"]) == (
            set(type(card).model_fields) - type(card)._STRUCTURAL_FIELDS
        )
        assert "id" not in wire["props"]

    def test_heading_levels_render_below_the_page_title(self):
        spec = GenerativeUISpec.model_validate(
            {
                "root": [
                    {"component": "Heading", "value": "a", "level": 1},
                    {"component": "Heading", "value": "b", "level": 2},
                    {"component": "Heading", "value": "c", "level": 3},
                ]
            }
        )
        assert spec.to_text() == "## a\n### b\n#### c"

    def test_alert_without_a_title_omits_the_dash(self):
        node = GenerativeUISpec.model_validate(
            {"root": [{"component": "Alert", "body": "disk is full", "variant": "danger"}]}
        ).root[0]
        assert node.to_text() == "DANGER: disk is full"

    def test_table_cells_cannot_break_out_of_the_markdown_table(self):
        node = GenerativeUISpec.model_validate(
            {
                "root": [
                    {
                        "component": "Table",
                        "columns": ["a"],
                        "rows": [["one | two"], ["line\nbreak"]],
                    }
                ]
            }
        ).root[0]
        assert node.to_text() == "| a |\n| --- |\n| one \\| two |\n| line break |"

    def test_codeblock_without_a_language_still_fences(self):
        node = GenerativeUISpec.model_validate(
            {"root": [{"component": "CodeBlock", "code": "x = 1"}]}
        ).root[0]
        assert node.to_text() == "```\nx = 1\n```"


# ---------------------------------------------------------------------------
# Link hrefs — the security boundary
# ---------------------------------------------------------------------------


class TestLinkHrefScheme:
    @pytest.mark.parametrize(
        "href",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "java\tscript:alert(1)",  # browsers strip the tab; so must we
            "data:text/html;base64,PHNjcmlwdD4=",
            "file:///etc/passwd",
            "//evil.example.com/path",  # scheme-relative: inherits the console's
            "/relative/path",
            "vbscript:msgbox",
            "https://",  # scheme, no destination
            "mailto:",  # scheme, no destination
        ],
    )
    def test_unsafe_or_incomplete_href_is_refused(self, href):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "Link", "href": href, "label": "click"}]}
            )

    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com",
            "https://example.com/a/b?c=d#e",
            "http://example.com:8080/x",
            "mailto:someone@example.com",
        ],
    )
    def test_supported_schemes_are_admitted(self, href):
        spec = GenerativeUISpec.model_validate(
            {"root": [{"component": "Link", "href": href, "label": "click"}]}
        )
        assert spec.root[0].href == href


# ---------------------------------------------------------------------------
# Per-variant refusals
# ---------------------------------------------------------------------------


class TestVariantRefusals:
    @pytest.mark.parametrize(
        "rows",
        [
            [["only-one"]],  # short row
            [["a", "b", "c"]],  # long row
            [["a", "b"], ["c"]],  # one good row, one ragged
        ],
    )
    def test_ragged_table_rows_are_refused_never_padded(self, rows):
        """Padding renders a confidently wrong table — the one failure a data
        component must not have."""
        with pytest.raises(ValidationError, match="expected 2"):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "Table", "columns": ["a", "b"], "rows": rows}]}
            )

    @pytest.mark.parametrize("columns", [[], ["a"] * 9])
    def test_table_column_count_is_bounded(self, columns):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "Table", "columns": columns, "rows": []}]}
            )

    def test_table_row_count_is_bounded(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {
                    "root": [
                        {"component": "Table", "columns": ["a"], "rows": [["x"]] * 51}
                    ]
                }
            )

    @pytest.mark.parametrize("count", [0, 21])
    def test_keyvalue_item_count_is_bounded(self, count):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {
                    "root": [
                        {
                            "component": "KeyValue",
                            "items": [{"label": "k", "value": "v"}] * count,
                        }
                    ]
                }
            )

    @pytest.mark.parametrize(
        "component", ["Text", "Heading", "Badge", "Table", "CodeBlock", "Alert", "Divider", "Link"]
    )
    def test_a_leaf_carrying_children_is_refused(self, component):
        """No validator does this — the leaf has no ``children`` field, so
        ``extra="forbid"`` refuses it structurally and a twelfth leaf inherits
        the guard for free."""
        node: dict[str, object] = {"component": component, "children": []}
        node.update(
            {
                "Text": {"value": "x"},
                "Heading": {"value": "x"},
                "Badge": {"label": "x"},
                "Table": {"columns": ["a"]},
                "CodeBlock": {"code": "x"},
                "Alert": {"body": "x"},
                "Divider": {},
                "Link": {"href": "https://example.com", "label": "x"},
            }[component]
        )
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate({"root": [node]})

    def test_an_unknown_component_is_refused(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate({"root": [{"component": "Chart", "data": []}]})

    def test_an_unknown_prop_is_refused(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "Text", "value": "x", "colour": "red"}]}
            )

    def test_an_invalid_language_tag_is_refused(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "CodeBlock", "code": "x", "language": "py\n```"}]}
            )


# ---------------------------------------------------------------------------
# String caps + normalisation
# ---------------------------------------------------------------------------


class TestStringCaps:
    @pytest.mark.parametrize(
        ("node", "field", "limit"),
        [
            ({"component": "Text", "value": ""}, "value", PROSE_MAX_CHARS),
            ({"component": "Heading", "value": ""}, "value", LABEL_MAX_CHARS),
            ({"component": "Badge", "label": ""}, "label", LABEL_MAX_CHARS),
            ({"component": "Alert", "body": ""}, "body", PROSE_MAX_CHARS),
            ({"component": "CodeBlock", "code": ""}, "code", CODE_MAX_CHARS),
        ],
    )
    def test_field_is_capped_at_its_declared_limit(self, node, field, limit):
        at_limit = dict(node, **{field: "x" * limit})
        GenerativeUISpec.model_validate({"root": [at_limit]})

        over_limit = dict(node, **{field: "x" * (limit + 1)})
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate({"root": [over_limit]})

    @pytest.mark.parametrize(
        "node",
        [
            {"component": "Text", "value": "   "},
            {"component": "Heading", "value": "\n\t "},
            {"component": "Badge", "label": " "},
            {"component": "Alert", "body": "  "},
            {"component": "Link", "href": "https://example.com", "label": " "},
        ],
    )
    def test_a_whitespace_only_label_is_empty(self, node):
        """``strip_whitespace`` runs BEFORE the length check, which is what
        makes ``min_length=1`` mean 'non-empty after strip'."""
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate({"root": [node]})

    def test_surrounding_whitespace_is_normalised_away(self):
        spec = GenerativeUISpec.model_validate(
            {"root": [{"component": "Text", "value": "  hello  "}]}
        )
        assert spec.root[0].value == "hello"

    def test_code_indentation_survives_because_code_is_never_stripped(self):
        spec = GenerativeUISpec.model_validate(
            {"root": [{"component": "CodeBlock", "code": "    indented\n"}]}
        )
        assert spec.root[0].code == "    indented\n"


# ---------------------------------------------------------------------------
# Tree-level limits
# ---------------------------------------------------------------------------


class TestTreeLimits:
    @staticmethod
    def _nest(depth: int) -> dict[str, object]:
        node: dict[str, object] = {"component": "Text", "value": "leaf"}
        for _ in range(depth - 1):
            node = {"component": "Stack", "children": [node]}
        return node

    def test_a_tree_at_the_depth_limit_is_accepted(self):
        spec = GenerativeUISpec.model_validate({"root": [self._nest(MAX_TREE_DEPTH)]})
        assert spec.measure()[0] == MAX_TREE_DEPTH

    def test_a_tree_past_the_depth_limit_is_refused(self):
        with pytest.raises(ValidationError, match="levels deep"):
            GenerativeUISpec.model_validate({"root": [self._nest(MAX_TREE_DEPTH + 1)]})

    def test_a_tree_at_the_node_limit_is_accepted(self):
        root = [{"component": "Divider"} for _ in range(MAX_TREE_NODES)]
        assert GenerativeUISpec.model_validate({"root": root}).measure()[1] == MAX_TREE_NODES

    def test_a_tree_past_the_node_limit_is_refused(self):
        """Counted across the WHOLE tree, not per level — a container holding
        the overflow must not slip past a per-list ``max_length``."""
        root = [
            {
                "component": "Stack",
                "children": [{"component": "Divider"} for _ in range(MAX_TREE_NODES)],
            }
        ]
        with pytest.raises(ValidationError, match="nodes, limit"):
            GenerativeUISpec.model_validate({"root": root})

    def test_a_tree_past_the_serialized_size_limit_is_refused(self):
        """The per-field caps alone do not bound the whole: this payload is
        persisted and replayed to every client on every history read."""
        blocks = (MAX_SPEC_CHARS // CODE_MAX_CHARS) + 2
        root = [{"component": "CodeBlock", "code": "x" * CODE_MAX_CHARS} for _ in range(blocks)]
        with pytest.raises(ValidationError, match="characters, limit"):
            GenerativeUISpec.model_validate({"root": root})

    def test_an_empty_tree_is_refused(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate({"root": []})

    def test_the_spec_forbids_extra_keys(self):
        with pytest.raises(ValidationError):
            GenerativeUISpec.model_validate(
                {"root": [{"component": "Divider"}], "theme": "dark"}
            )

    def test_measure_counts_containers_as_nodes(self):
        spec = GenerativeUISpec.model_validate(
            {
                "root": [
                    {
                        "component": "Card",
                        "children": [
                            {"component": "Divider"},
                            {"component": "Divider"},
                        ],
                    }
                ]
            }
        )
        assert spec.measure() == (2, 3)
