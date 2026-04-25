# LSP Code Intelligence

Truss ships a native Language Server Protocol tool that gives the AI real code intelligence. It covers diagnostics, go-to-definition, find-references, and hover information. Servers are auto-discovered on your `PATH` and start lazily on first use. After every file edit, diagnostics run automatically and land in the AI's context. It sees compile errors and type issues in the same turn it made the change.

## Built-in language servers

| Language | Server | Install |
|----------|--------|---------|
| Python | `pyright` (pyright-langserver) | `pip install pyright` |
| TypeScript / JavaScript | `typescript-language-server` | `npm install -g typescript-language-server typescript` |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |

Just have the binary on your `PATH`. Servers that are not installed are silently skipped; no configuration change is needed.

## Operations

| Operation | Required fields | What it returns |
|-----------|----------------|-----------------|
| `diagnostics` | `file_path` | Errors and warnings in a file (capped at 50, severity ≥ warning) |
| `definition` | `file_path`, `line`, `character` | File path(s) where the symbol is defined |
| `references` | `file_path`, `line`, `character` | All reference locations including the declaration |
| `hover` | `file_path`, `line`, `character` | Type signature and documentation for the symbol |

`line` and `character` are **0-based** (first line = 0, first column = 0).

**Example.** Ask for diagnostics in a Python file:

```json
{
  "operation": "diagnostics",
  "file_path": "/home/user/project/main.py"
}
```

**Example.** Jump to definition at line 42, column 12:

```json
{
  "operation": "definition",
  "file_path": "/home/user/project/main.py",
  "line": 41,
  "character": 12
}
```

## Passive diagnostics

The LSP tool runs automatically after every file edit and appends type errors and lint warnings to the AI's context. No explicit tool call is needed. To turn this off, set `agent.lsp.enabled` to `false`.

## Workspace root

Each server picks a workspace root by walking up from the current working directory and looking for well-known marker files.

| Server | Root markers |
|--------|-------------|
| pyright | `pyproject.toml`, `setup.py`, `setup.cfg`, `pyrightconfig.json` |
| typescript-language-server | `tsconfig.json`, `package.json` |
| gopls | `go.mod` |
| rust-analyzer | `Cargo.toml` |

If no marker is found nearby, the current working directory is used as the root.

## Configuration

### Enable / disable

LSP is enabled by default. To disable it entirely:

```json
{
  "agent": {
    "lsp": {
      "enabled": false
    }
  }
}
```

### Disable a specific server

```json
{
  "agent": {
    "lsp": {
      "servers": {
        "pyright": { "disabled": true }
      }
    }
  }
}
```

### Add a custom language server

Define a custom server under `agent.lsp.servers`. All fields except `command` and `extensions` are optional.

```json
{
  "agent": {
    "lsp": {
      "servers": {
        "my-lsp": {
          "command": ["my-language-server", "--stdio"],
          "extensions": [".mylang", ".ml"],
          "root_markers": ["my-project.toml"],
          "language_id": "mylang"
        }
      }
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `command` | Yes | List of binary and arguments (must be on `PATH`). |
| `extensions` | Yes | List of file extensions this server handles (e.g. `[".py"]`). |
| `root_markers` | No | Filenames that indicate a workspace root. |
| `language_id` | No | LSP `languageId` string (defaults to the server's key name). |
| `disabled` | No | Set `true` to disable a built-in server. |

### Full config key reference

All keys are nested under `agent.lsp` in `configs/app.json`.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable the LSP tool and passive diagnostics. |
| `servers` | `{}` | Per-server overrides and custom server definitions. |

## Installation

The LSP tool depends on optional packages. Install them with:

```bash
uv sync --extra lsp
```

When those dependencies are absent, the LSP tool is silently disabled. There is no crash and no error on startup.

---

> [!NOTE] How it works internally
> See [Architecture Overview → LSP tool](core-orchestration.md#lsp).
