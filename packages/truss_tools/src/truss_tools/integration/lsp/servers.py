"""Built-in language server definitions."""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerDef:
    """A language server that can be spawned for files matching *extensions*."""

    id: str
    extensions: tuple[str, ...]
    command: tuple[str, ...]
    root_markers: tuple[str, ...]
    language_id: str  # LSP languageId string


BUILTIN_SERVERS: tuple[ServerDef, ...] = (
    ServerDef(
        id="pyright",
        extensions=(".py", ".pyi"),
        command=("pyright-langserver", "--stdio"),
        root_markers=("pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json"),
        language_id="python",
    ),
    ServerDef(
        id="typescript-language-server",
        extensions=(".ts", ".tsx", ".js", ".jsx"),
        command=("typescript-language-server", "--stdio"),
        root_markers=("tsconfig.json", "package.json"),
        language_id="typescript",
    ),
    ServerDef(
        id="gopls",
        extensions=(".go",),
        command=("gopls", "serve"),
        root_markers=("go.mod",),
        language_id="go",
    ),
    ServerDef(
        id="rust-analyzer",
        extensions=(".rs",),
        command=("rust-analyzer",),
        root_markers=("Cargo.toml",),
        language_id="rust",
    ),
)

# Extension → language_id for languages without a running server
_EXTENSION_LANGUAGE_MAP: dict[str, str] = {}
for _s in BUILTIN_SERVERS:
    for _ext in _s.extensions:
        _EXTENSION_LANGUAGE_MAP.setdefault(_ext, _s.language_id)


def available_servers(
    overrides: dict[str, dict] | None = None,
) -> list[ServerDef]:
    """Return servers whose command binary is installed on the system.

    *overrides* can disable built-in servers (``{"pyright": {"disabled": true}}``)
    or add custom ones (``{"my-lsp": {"command": [...], "extensions": [...], ...}}``).
    """
    overrides = overrides or {}
    result: list[ServerDef] = []

    for server in BUILTIN_SERVERS:
        ovr = overrides.get(server.id, {})
        if ovr.get("disabled"):
            continue
        if shutil.which(server.command[0]):
            result.append(server)

    # User-defined servers
    for name, cfg in overrides.items():
        if any(s.id == name for s in BUILTIN_SERVERS):
            continue  # already handled above
        if cfg.get("disabled"):
            continue
        cmd = cfg.get("command")
        exts = cfg.get("extensions")
        if not cmd or not exts:
            continue
        cmd_tuple = tuple(cmd) if isinstance(cmd, list) else (cmd,)
        if not shutil.which(cmd_tuple[0]):
            continue
        result.append(
            ServerDef(
                id=name,
                extensions=tuple(exts),
                command=cmd_tuple,
                root_markers=tuple(cfg.get("root_markers", [])),
                language_id=cfg.get("language_id", name),
            )
        )

    return result
