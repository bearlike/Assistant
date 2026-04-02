#!/usr/bin/env python3
"""Generate JSON Schema from the AppConfig Pydantic model.

Performs an AST pre-check to verify that the AppConfig class exists in the
source file before importing it.  Writes the schema to ``configs/app.schema.json``
and exits with code 0 (unchanged) or 1 (updated / error).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_MODULE_PATH = (
    REPO_ROOT
    / "packages"
    / "meeseeks_core"
    / "src"
    / "meeseeks_core"
    / "config.py"
)
SCHEMA_OUTPUT_PATH = REPO_ROOT / "configs" / "app.schema.json"
SCHEMA_ID = (
    "https://thekrishna.in/Assistant/latest/app.schema.json"
)


# ---------------------------------------------------------------------------
# 1. AST guard – ensure AppConfig class still exists in the source
# ---------------------------------------------------------------------------
def _ast_check() -> None:
    source = CONFIG_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONFIG_MODULE_PATH))
    class_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    if "AppConfig" not in class_names:
        raise RuntimeError(
            f"AppConfig class not found in {CONFIG_MODULE_PATH}. "
            "The config model may have been renamed or removed — update this script."
        )


# ---------------------------------------------------------------------------
# 2. Generate schema via native Pydantic mechanism
# ---------------------------------------------------------------------------
def _generate_schema() -> str:
    from meeseeks_core.config import AppConfig

    schema = AppConfig.model_json_schema()
    # Place $id first for readability; JSON Schema processors
    # are order-agnostic but humans read top-down.
    ordered = {"$id": SCHEMA_ID, **schema}
    return json.dumps(ordered, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 3. Compare and write
# ---------------------------------------------------------------------------
def main() -> int:
    """Generate the AppConfig JSON schema and write it to disk."""
    _ast_check()
    new_schema = _generate_schema()

    if SCHEMA_OUTPUT_PATH.exists():
        old_schema = SCHEMA_OUTPUT_PATH.read_text(encoding="utf-8")
        if old_schema == new_schema:
            print(f"Schema unchanged: {SCHEMA_OUTPUT_PATH}")
            return 0

    SCHEMA_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_OUTPUT_PATH.write_text(new_schema, encoding="utf-8")
    print(f"Schema updated: {SCHEMA_OUTPUT_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
