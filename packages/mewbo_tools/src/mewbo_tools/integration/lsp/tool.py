"""LSP tool — agent-facing interface for language server queries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lsprotocol import types
from mewbo_core.classes import AbstractTool, ActionStep
from mewbo_core.common import MockSpeaker

from mewbo_tools.integration.lsp import get_lsp_manager, run_lsp_async

# Tool metadata schema exposed to the LLM
LSP_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["diagnostics", "definition", "references", "hover"],
            "description": "LSP operation to perform",
        },
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file",
        },
        "line": {
            "type": "integer",
            "description": "0-based line number (required for definition/references/hover)",
        },
        "character": {
            "type": "integer",
            "description": "0-based column (required for definition/references/hover)",
        },
    },
    "required": ["operation", "file_path"],
}

_SEVERITY_LABELS = {
    types.DiagnosticSeverity.Error: "error",
    types.DiagnosticSeverity.Warning: "warning",
    types.DiagnosticSeverity.Information: "info",
    types.DiagnosticSeverity.Hint: "hint",
}


class LSPTool(AbstractTool):
    """Query language servers for code diagnostics and navigation."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107
        super().__init__(
            name="Language Server",
            description="Query language servers for code intelligence.",
            use_llm=False,
            **kwargs,
        )

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Dispatch to the requested LSP operation."""
        tool_input = action_step.tool_input or {}
        operation = tool_input.get("operation", "")
        file_path = tool_input.get("file_path", "")

        if not file_path:
            return MockSpeaker(content="file_path is required.")

        # Resolve relative paths against CWD
        resolved = str(Path(file_path).resolve())

        manager = get_lsp_manager(os.getcwd())

        if operation == "diagnostics":
            return self._do_diagnostics(manager, resolved)
        elif operation in ("definition", "references", "hover"):
            line = tool_input.get("line", 0)
            character = tool_input.get("character", 0)
            return self._do_navigation(manager, resolved, operation, line, character)
        else:
            return MockSpeaker(
                content=f"Unknown operation '{operation}'. "
                "Use: diagnostics, definition, references, hover"
            )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _do_diagnostics(self, manager: Any, file_path: str) -> MockSpeaker:
        """Get diagnostics for a file (open it first to trigger analysis)."""
        client = run_lsp_async(manager.ensure_server(file_path))
        if client is None:
            sdef = manager.server_for_file(file_path)
            if sdef is None:
                return MockSpeaker(
                    content=f"No language server configured for {Path(file_path).suffix} files."
                )
            return MockSpeaker(
                content=f"Language server '{sdef.id}' is not available. "
                f"Install it: {sdef.command[0]}"
            )

        # Open the file so the server analyzes it
        run_lsp_async(manager.open_file(client, file_path))

        # Give the server a moment to publish diagnostics
        import time

        time.sleep(2)

        diags = manager.get_cached_diagnostics(file_path)
        return MockSpeaker(content=self._format_diagnostics(file_path, diags, manager))

    def _do_navigation(
        self,
        manager: Any,
        file_path: str,
        operation: str,
        line: int,
        character: int,
    ) -> MockSpeaker:
        """Handle definition/references/hover."""
        client = run_lsp_async(manager.ensure_server(file_path))
        if client is None:
            return MockSpeaker(
                content=f"No language server available for {Path(file_path).suffix} files."
            )

        # Ensure file is open
        run_lsp_async(manager.open_file(client, file_path))

        uri = Path(file_path).resolve().as_uri()
        pos = types.Position(line=line, character=character)
        text_doc = types.TextDocumentIdentifier(uri=uri)

        try:
            if operation == "definition":
                result = run_lsp_async(
                    client.text_document_definition_async(
                        types.DefinitionParams(text_document=text_doc, position=pos)
                    )
                )
                return MockSpeaker(content=self._format_locations(result, "Definition"))

            elif operation == "references":
                result = run_lsp_async(
                    client.text_document_references_async(
                        types.ReferenceParams(
                            text_document=text_doc,
                            position=pos,
                            context=types.ReferenceContext(include_declaration=True),
                        )
                    )
                )
                return MockSpeaker(content=self._format_locations(result, "References"))

            elif operation == "hover":
                result = run_lsp_async(
                    client.text_document_hover_async(
                        types.HoverParams(text_document=text_doc, position=pos)
                    )
                )
                return MockSpeaker(content=self._format_hover(result))

        except Exception as exc:
            return MockSpeaker(content=f"LSP {operation} failed: {exc}")

        return MockSpeaker(content=f"Unknown operation: {operation}")

    # ------------------------------------------------------------------
    # Formatting (LLM-friendly, not raw JSON)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_diagnostics(
        file_path: str,
        diags: list[types.Diagnostic],
        manager: Any,
    ) -> str:
        sdef = manager.server_for_file(file_path)
        server_name = sdef.id if sdef else "unknown"
        rel = Path(file_path).name

        if not diags:
            return f"No diagnostics for {rel} ({server_name}) — file looks clean."

        # Filter to errors and warnings only
        relevant = [
            d
            for d in diags
            if d.severity
            in (
                types.DiagnosticSeverity.Error,
                types.DiagnosticSeverity.Warning,
                None,  # some servers omit severity
            )
        ]
        if not relevant:
            return (
                f"No errors or warnings for {rel} ({server_name}). "
                f"{len(diags)} hints/info diagnostics suppressed."
            )

        lines: list[str] = [f"# Diagnostics for {rel} ({server_name})\n"]
        errors = warnings = 0
        for d in relevant[:50]:  # cap at 50 to avoid context bloat
            sev = _SEVERITY_LABELS.get(d.severity, "error") if d.severity else "error"
            if sev == "error":
                errors += 1
            else:
                warnings += 1
            code_str = f" ({d.code})" if d.code else ""
            lines.append(f"line {d.range.start.line + 1}: {sev} — {d.message}{code_str}")

        lines.append(f"\n{errors} error(s), {warnings} warning(s)")
        if len(relevant) > 50:
            lines.append(f"({len(relevant) - 50} more suppressed)")
        return "\n".join(lines)

    @staticmethod
    def _format_locations(
        result: types.Location | list[types.Location] | list[types.LocationLink] | None,
        label: str,
    ) -> str:
        if result is None:
            return f"No {label.lower()} found."

        locations: list[types.Location | types.LocationLink] = (
            [result] if isinstance(result, types.Location) else list(result)
        )
        if not locations:
            return f"No {label.lower()} found."

        lines: list[str] = [f"# {label} ({len(locations)} result(s))\n"]
        for loc in locations[:20]:  # cap
            if isinstance(loc, types.LocationLink):
                uri = loc.target_uri
                rng = loc.target_range
            else:
                uri = loc.uri
                rng = loc.range

            # Convert file URI to path
            path = uri.replace("file://", "")
            line_num = rng.start.line + 1
            lines.append(f"{path}:{line_num}")

        if len(locations) > 20:
            lines.append(f"({len(locations) - 20} more)")
        return "\n".join(lines)

    @staticmethod
    def _format_hover(result: types.Hover | None) -> str:
        if result is None:
            return "No hover information available."

        content = result.contents
        if isinstance(content, types.MarkupContent):
            return content.value
        elif isinstance(content, str):
            return content
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif hasattr(item, "value"):
                    parts.append(item.value)
            return "\n".join(parts) if parts else "No hover information."
        return str(content)
