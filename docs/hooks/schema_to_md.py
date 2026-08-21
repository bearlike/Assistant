"""MkDocs on_pre_build hook: generates docs/configuration.md from configs/app.schema.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("mkdocs.hooks.schema_to_md")

SCHEMA_PATH = Path("configs/app.schema.json")
OUTPUT_PATH = Path("docs/configuration.md")

# Repo-file references use the `repo:` badge scheme (rewritten to badges by
# the installed mkdocs-shadcn theme's code_refs plugin);
# `app.json` is the user-created, gitignored config — NOT a committed artifact —
# so it stays inline code rather than a badge that would 404 on GitHub.
# The H1 has to be the file's first block. The theme lifts the leading H1 out of
# the body to render it as the page's label, and it only recognises it in that
# position, so a comment above it left the heading in the body and the page
# rendered its own title twice.
HEADER = """\
# Configuration Reference

## Every configuration key

<!-- AUTO-GENERATED from configs/app.schema.json. Do not edit manually. -->

Mewbo is configured via `configs/app.json`. This reference is auto-generated
from the JSON Schema at [`configs/app.schema.json`](repo:configs/app.schema.json).

Copy [`configs/app.example.json`](repo:configs/app.example.json) to
`configs/app.json` to get started.
See [Get Started](getting-started.md) for the full setup walkthrough.
"""


def _resolve_ref(ref: str, defs: dict) -> dict:
    """Resolve a $ref like '#/$defs/LLMConfig' to its definition dict."""
    if ref.startswith("#/$defs/"):
        name = ref[len("#/$defs/") :]
        return defs.get(name, {})
    return {}


def _type_label(prop: dict, defs: dict) -> str:
    """Return a short human-readable type string for a property."""
    if "$ref" in prop:
        ref_def = _resolve_ref(prop["$ref"], defs)
        return ref_def.get("title", prop["$ref"].split("/")[-1])

    any_of = prop.get("anyOf", [])
    if any_of:
        # Strip null from anyOf to get the real type
        non_null = [t for t in any_of if t.get("type") != "null"]
        if len(non_null) == 1:
            return _type_label(non_null[0], defs)
        return " | ".join(_type_label(t, defs) for t in non_null)

    prop_type = prop.get("type", "")
    if prop_type == "array":
        items = prop.get("items", {})
        inner = _type_label(items, defs) if items else "any"
        return f"list[{inner}]"
    if prop_type == "object":
        additional = prop.get("additionalProperties")
        if isinstance(additional, dict):
            inner = _type_label(additional, defs)
            return f"dict[str, {inner}]"
        return "object"
    return prop_type or "any"


def _default_label(prop: dict) -> str:
    """Return the default value as a code-formatted string, or empty.

    A field marked ``x-secret`` never renders its value, whatever the schema
    carries. Today every secret's default happens to be the empty string, so
    this changes almost nothing — which is exactly why it belongs here rather
    than at a call site: the redaction has to hold for the field ADDED later
    with a real placeholder in it. This reference is a published artifact
    regenerated on every docs build, so a value that reaches it republishes
    itself until someone notices.
    """
    if prop.get("x-secret"):
        return ""
    if "default" not in prop:
        return ""
    val = prop["default"]
    if val is None:
        return "`null`"
    if val == "":
        return '`""`'
    if isinstance(val, bool):
        return f"`{str(val).lower()}`"
    if isinstance(val, int | float):
        return f"`{val}`"
    if isinstance(val, list):
        if not val:
            return "`[]`"
        return f"`{json.dumps(val)}`"
    return f"`{val}`"


def _escape_pipe(s: str) -> str:
    """Escape pipe characters so they don't break markdown tables."""
    return s.replace("|", "&#124;")


def _table_cell(s: str) -> str:
    """Make a description safe to interpolate into a single markdown table row.

    A table row is one line of markdown: a literal newline inside a cell ends
    the row early and corrupts every row after it in the section. The
    console's ``FieldHelp`` contract deliberately shapes a description as a
    summary line, a blank line, then narrative — that two-paragraph text is
    correct for the Settings UI popover, so the fix belongs here rather than
    in the source description. ``md_in_html`` is already enabled
    (``mkdocs.yml``), so a literal ``<br>`` renders inside a table cell same
    as the rest of this page already relies on raw HTML passing through.
    """
    return _escape_pipe(s).replace("\n\n", "<br><br>").replace("\n", "<br>")


# Nesting is shallow by design (the deepest config submodel sits three levels
# below its section), so this bound only exists to keep a future schema change
# from turning a build into a runaway walk. The cycle guard below is the real
# protection; this is the belt to its braces.
_MAX_NESTING_DEPTH = 4


def _submodel(prop: dict, defs: dict) -> dict | None:
    """Return the nested object model a property points at, if it is one.

    Only a direct ``$ref`` (or a ``$ref`` inside ``anyOf``, which is how an
    optional submodel is spelled) counts. A ``list[X]`` or ``dict[str, X]`` is
    deliberately NOT followed: its entries vary, so there is no fixed set of
    keys to document — the type label already says what the entries are, and
    the schema source carries their shape.
    """
    if "$ref" in prop:
        ref_def = _resolve_ref(prop["$ref"], defs)
        return ref_def if ref_def.get("properties") else None
    for option in prop.get("anyOf", []):
        if "$ref" in option:
            ref_def = _resolve_ref(option["$ref"], defs)
            if ref_def.get("properties"):
                return ref_def
    return None


def _description(prop: dict, defs: dict) -> str:
    """The property's description, falling back to its submodel's own.

    A property that is just a ``$ref`` to a submodel usually carries no
    description of its own — the prose lives on the referenced model. Without
    this fallback the parent row of a nested block renders with an empty
    Description cell, which is the one row a reader needs to understand what
    the indented keys under it are for.
    """
    own = prop.get("description", "").strip()
    if own:
        return own
    submodel = _submodel(prop, defs)
    return submodel.get("description", "").strip() if submodel else ""


def _flatten_properties(
    properties: dict,
    defs: dict,
    *,
    prefix: str = "",
    seen: frozenset[str] = frozenset(),
    depth: int = 0,
) -> list[tuple[str, dict]]:
    """Flatten a model's properties, walking into nested submodels.

    Returns ``(dotted_key, property)`` pairs — ``session.ttl_seconds`` rather
    than a nested table — so a section stays one table no matter how deep its
    model tree goes. A submodel's children follow immediately after their
    parent row, which keeps the parent's type and description as the heading
    for the block that belongs to it.

    Without this walk the reference documents only each section's DIRECT
    properties, so an entire subsystem configured through a submodel renders
    as one opaque row and none of its keys appear anywhere.

    ``seen`` carries the submodel titles already open on this path, so a schema
    that ever refers back to an ancestor stops instead of recursing forever.
    """
    rows: list[tuple[str, dict]] = []
    for key, prop in properties.items():
        path = f"{prefix}{key}"
        rows.append((path, prop))
        submodel = _submodel(prop, defs)
        if submodel is None or depth >= _MAX_NESTING_DEPTH:
            continue
        title = submodel.get("title", path)
        if title in seen:
            continue
        rows.extend(
            _flatten_properties(
                submodel.get("properties", {}),
                defs,
                prefix=f"{path}.",
                seen=seen | {title},
                depth=depth + 1,
            )
        )
    return rows


def _render_class_section(
    section_key: str,
    class_def: dict,
    defs: dict,
) -> str:
    """Render a ## section for one top-level config group."""
    title = class_def.get("title", section_key)
    description = class_def.get("description", "").strip()
    properties = class_def.get("properties", {})

    lines: list[str] = []
    lines.append(f"## {title}\n")
    lines.append(f"Top-level key: `{section_key}`\n")
    if description:
        lines.append(f"{description}\n")

    if not properties:
        lines.append("_No configurable properties._\n")
        return "\n".join(lines)

    # Separate deprecated from active properties. Nested submodel keys are
    # flattened to dotted paths first, so they sort into the same two tables
    # as the section's own properties.
    active: list[tuple[str, dict]] = []
    deprecated: list[tuple[str, dict]] = []
    for key, prop in _flatten_properties(properties, defs):
        desc = prop.get("description", "")
        if "deprecated" in desc.lower():
            deprecated.append((key, prop))
        else:
            active.append((key, prop))

    if active:
        lines.append("| Key | Type | Default | Description |")
        lines.append("| --- | ---- | ------- | ----------- |")
        for key, prop in active:
            type_str = _type_label(prop, defs)
            default_str = _default_label(prop)
            desc = _description(prop, defs)
            # ⚠️ marks fields the REST API never reads back: x-protected
            # (never read, never written) and x-secret (write-only).
            if prop.get("x-protected") or prop.get("x-secret"):
                desc = f"{desc} ⚠️" if desc else "⚠️"
            lines.append(
                f"| `{key}` | {_escape_pipe(type_str)} | {default_str} | {_table_cell(desc)} |"
            )
        lines.append("")

    if deprecated:
        lines.append('??? note "Deprecated fields"\n')
        lines.append("    | Key | Type | Default | Description |")
        lines.append("    | --- | ---- | ------- | ----------- |")
        for key, prop in deprecated:
            type_str = _type_label(prop, defs)
            default_str = _default_label(prop)
            desc = _description(prop, defs)
            lines.append(
                f"    | `{key}` | {_escape_pipe(type_str)} | {default_str} | {_table_cell(desc)} |"
            )
        lines.append("")

    return "\n".join(lines)


def render_markdown(schema: dict) -> str:
    """Render the whole reference page from a parsed schema, writing nothing.

    Split out of :func:`on_pre_build` so the page can be BUILT without being
    written: a freshness test that had to regenerate first would touch the repo
    as a side effect, and could "pass" by overwriting the very drift it exists
    to report. ``configuration.md`` is a committed file generated only at
    docs-build time, so nothing else notices when it falls behind the schema.
    """
    defs = schema.get("$defs", {})
    top_level_props = schema.get("properties", {})

    sections: list[str] = [HEADER]

    for key, prop_def in top_level_props.items():
        # Resolve the class definition for this top-level key
        if "$ref" in prop_def:
            class_def = _resolve_ref(prop_def["$ref"], defs)
        elif "anyOf" in prop_def:
            # Pick the first non-null ref
            non_null = [t for t in prop_def["anyOf"] if "$ref" in t and t.get("type") != "null"]
            class_def = _resolve_ref(non_null[0]["$ref"], defs) if non_null else {}
        else:
            # Inline definition (e.g. channels, projects). Render a lightweight stub
            inline_title = prop_def.get("title", key.replace("_", " ").title())
            inline_desc = prop_def.get("description", "").strip()
            stub_lines = [f"## {inline_title}\n", f"Top-level key: `{key}`\n"]
            if inline_desc:
                stub_lines.append(f"{inline_desc}\n")
            stub_lines.append("_Structure varies by entry. See the schema source for details._\n")
            sections.append("\n".join(stub_lines))
            continue

        if not class_def:
            continue

        sections.append(_render_class_section(key, class_def, defs))

    return "\n".join(sections)


def on_pre_build(**_: object) -> None:
    """MkDocs hook: regenerate docs/configuration.md before each build."""
    if not SCHEMA_PATH.exists():
        log.warning(
            "schema_to_md: %s not found; skipping configuration.md generation",
            SCHEMA_PATH,
        )
        return

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("schema_to_md: failed to parse %s: %s", SCHEMA_PATH, exc)
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_content = render_markdown(schema)
    if OUTPUT_PATH.exists() and OUTPUT_PATH.read_text(encoding="utf-8") == new_content:
        log.debug("schema_to_md: %s unchanged; skipping write", OUTPUT_PATH)
        return
    OUTPUT_PATH.write_text(new_content, encoding="utf-8")
    log.info("schema_to_md: wrote %s", OUTPUT_PATH)
