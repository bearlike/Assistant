"""MkDocs on_pre_build hook: generates docs/configuration.md from configs/app.schema.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("mkdocs.hooks.schema_to_md")

SCHEMA_PATH = Path("configs/app.schema.json")
OUTPUT_PATH = Path("docs/configuration.md")

_SCHEMA_URL = "https://github.com/bearlike/Assistant/blob/main/configs/app.schema.json"
HEADER = f"""\
<!-- AUTO-GENERATED from configs/app.schema.json — do not edit manually -->
# Configuration Reference

Meeseeks is configured via `configs/app.json`. This reference is auto-generated
from the JSON Schema at [`configs/app.schema.json`]({_SCHEMA_URL}).

Copy `configs/app.example.json` to `configs/app.json` to get started.
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
    """Return the default value as a code-formatted string, or empty."""
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

    # Separate deprecated from active properties
    active: list[tuple[str, dict]] = []
    deprecated: list[tuple[str, dict]] = []
    for key, prop in properties.items():
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
            desc = prop.get("description", "").strip()
            if prop.get("x-protected"):
                desc = f"{desc} ⚠️" if desc else "⚠️"
            lines.append(
                f"| `{key}` | {_escape_pipe(type_str)} | {default_str} | {_escape_pipe(desc)} |"
            )
        lines.append("")

    if deprecated:
        lines.append('??? note "Deprecated fields"\n')
        lines.append("    | Key | Type | Default | Description |")
        lines.append("    | --- | ---- | ------- | ----------- |")
        for key, prop in deprecated:
            type_str = _type_label(prop, defs)
            default_str = _default_label(prop)
            desc = prop.get("description", "").strip()
            lines.append(
                f"    | `{key}` | {_escape_pipe(type_str)} | {default_str} | {_escape_pipe(desc)} |"
            )
        lines.append("")

    return "\n".join(lines)


def on_pre_build(**_: object) -> None:
    """MkDocs hook: regenerate docs/configuration.md before each build."""
    if not SCHEMA_PATH.exists():
        log.warning(
            "schema_to_md: %s not found — skipping configuration.md generation",
            SCHEMA_PATH,
        )
        return

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("schema_to_md: failed to parse %s: %s", SCHEMA_PATH, exc)
        return

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
            # Inline definition (e.g. channels, projects) — render a lightweight stub
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

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_content = "\n".join(sections)
    if OUTPUT_PATH.exists() and OUTPUT_PATH.read_text(encoding="utf-8") == new_content:
        log.debug("schema_to_md: %s unchanged — skipping write", OUTPUT_PATH)
        return
    OUTPUT_PATH.write_text(new_content, encoding="utf-8")
    log.info("schema_to_md: wrote %s", OUTPUT_PATH)
