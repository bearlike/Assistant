# Built-in Tools

Mewbo ships a set of first-party tools that every session has available by default. These tools cover the full local-development surface. You get reading files, editing them, running shell commands, and browsing directory trees. Because they are bundled with the core, no MCP server or external process is required. They activate the moment a session starts.

This page documents every built-in tool, its parameters, example output, and the configuration switches that control which editing backend is active.

For setup and installation, see [Getting Started](getting-started.md). For tool permissions and approval modes, see the [CLI client](clients-cli.md) page.

## Tool catalog

| Tool ID | Name | Read-only | Description |
|---|---|---|---|
| `read_file` | Read File | Yes | Line-windowed file reader |
| `aider_list_dir_tool` | List Directory | Yes | Recursive directory listing |
| `aider_shell_tool` | Shell | No | Run arbitrary shell commands |
| `aider_edit_block_tool` | Aider Edit Blocks | No | Apply Aider-style `SEARCH/REPLACE` blocks to files |
| `file_edit_tool` | File Edit | No | Exact string replacement with `old_string` / `new_string` |
| `home_assistant_tool` | Home Assistant | No | Smart home control (enabled when Home Assistant is configured) |
| `lsp_tool` | Language Server | Yes | Code diagnostics, go-to-definition, references, hover |

---

## read_file

`read_file` reads a local file and returns its content with 1-based line numbers, mirroring `cat -n` output. Reads are line-windowed: the default window is 2 000 lines, and `offset` / `limit` let the session page through arbitrarily large files without exhausting the context window. Mewbo also deduplicates repeated reads of the same slice so the same content does not consume context twice. In the web console, every `read_file` call renders as an expandable card in the session timeline showing the project, file path, and the exact line window the assistant pulled into context.

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-console-file-read-log.jpg" alt="A read_file tool card in the Mewbo console showing lines 81 through 90 of a 733-line litellm-config.yml with a truncated tag" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | Yes | File path to read (relative paths resolved against `root`) |
| `root` | string | No | Project root used for safe-path resolution (defaults to CWD) |
| `offset` | integer | No | 0-based starting line; defaults to `0` |
| `limit` | integer | No | Maximum lines to return; defaults to `2000` |

### Example output

```json
{
  "path": "src/main.py",
  "text": "1\tdef main():\n2\t    pass",
  "total_lines": 42
}
```

The `text` field contains the windowed content with 1-based line numbers separated by a tab. When the window is truncated, the final line reads `... (truncated - use offset/limit to read more)`.

### Example call

Read lines 200–400 of a large source file:

```json
{
  "path": "src/app/main.py",
  "offset": 199,
  "limit": 200
}
```

---

## File editing

Mewbo has two editing backends. Both apply edits atomically and return a unified diff so you can see exactly what changed. In the web console, every file edit is rendered as an expandable diff card in the session timeline, with line-level additions and deletions highlighted in green and red.

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-console-04-file-edit.jpg" alt="A file-edit tool card in the Mewbo console showing a unified diff with +23 additions and -2 deletions" style="width: 100%; max-width: 720px; height: auto;" />
</div>

The active backend for a session is chosen automatically based on the model, or you can pin it via [`agent.edit_tool`](configuration.md#agentconfig) in `configs/app.json`.

### search_replace_block (Aider-style)

`aider_edit_block_tool` parses one or more `SEARCH/REPLACE` blocks from a freeform text payload and applies them atomically.

**When to use:** models that prefer to write edits as prose text blocks. Claude models (Sonnet, Opus) default to this backend.

**Format:**

```
src/utils.py
```text
<<<<<<< SEARCH
def old_function():
    return 1
=======
def new_function():
    return 2
>>>>>>> REPLACE
```
```

Rules:

- The filename line must appear immediately before the opening fence.
- The `SEARCH` section must match the file content **exactly**, whitespace included.
- To skip unchanged sections inside a large block, use a line containing only `...` in both the `SEARCH` and `REPLACE` sections.
- Shell code blocks inside the content are rejected.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `content` | string | Yes | Full `SEARCH/REPLACE` block text (one or more blocks) |
| `root` | string | No | Project root for path resolution (defaults to CWD) |
| `files` | array of strings | No | Allowlist of filenames the tool may touch |

### structured_patch

`file_edit_tool` applies an exact string substitution to a single file.

**When to use:** models that prefer structured JSON tool calls (GPT-5, o-series, Codex, GPT-4) benefit from this format.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `file_path` | string | Yes | Path to the file to edit |
| `old_string` | string | Yes | Exact string to find and replace |
| `new_string` | string | Yes | Replacement string (may be empty to delete) |
| `replace_all` | boolean | No | Replace all occurrences; defaults to `false` |
| `root` | string | No | Project root for path resolution |

When `old_string` is empty, the tool **appends** `new_string` to the file (or creates the file if it does not exist). When `old_string` appears more than once and `replace_all` is `false`, the tool returns an error rather than applying an ambiguous edit.

**Example:**

```json
{
  "file_path": "src/utils.py",
  "old_string": "def old_function():\n    return 1",
  "new_string": "def new_function():\n    return 2"
}
```

---

## aider_shell_tool

`aider_shell_tool` runs an arbitrary shell command and returns stdout, stderr, exit code, and wall-clock duration. In the web console, every shell call renders as a terminal card in the session timeline with the command, the working directory, and the captured output so you can review exactly what the assistant ran.

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-console-shell-log.jpg" alt="A shell tool card in the Mewbo console showing a gh release list command with its JSON response and a 348ms duration" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string | Yes | Shell command to execute |
| `cwd` | string | No | Working directory (defaults to `root`, then CWD) |
| `root` | string | No | Project root used for safe-path resolution |

### Example output

```json
{
  "command": "pytest tests/ -q",
  "cwd": "/home/user/project",
  "exit_code": 0,
  "stdout": "5 passed in 0.42s",
  "stderr": "",
  "duration_ms": 423
}
```

### Behavior notes

- The command runs in a subprocess with the specified working directory. If `cwd` is outside the resolved `root`, the tool raises a path-validation error.
- Stdout and stderr are merged into the `stdout` field.
- Shell invocations never run in parallel with other write tools in the same step.
- Shell invocations require approval in the default permission policy. See the [CLI client](clients-cli.md) page for approval modes and auto-approve flags.

---

## aider_list_dir_tool

`aider_list_dir_tool` recursively lists all files under a directory and returns their paths relative to `root`.

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | Yes | Directory path to list |
| `root` | string | No | Project root (defaults to CWD); listed paths are relative to this |
| `max_entries` | integer | No | Maximum number of entries to return |

### Example output

```json
{
  "path": "src",
  "entries": ["src/main.py", "src/utils.py", "src/models/user.py"]
}
```

---

## Configuring the edit tool

When `agent.edit_tool` is empty (the default), Mewbo picks the right backend for the active model automatically. Override it only when you want to force a single backend regardless of which model is running.

| Value | Backend | When to use |
|---|---|---|
| `""` (empty, default) | Auto (chosen per model) | Recommended for mixed-model deployments |
| `"search_replace_block"` | `aider_edit_block_tool` | Force Aider format regardless of model |
| `"structured_patch"` | `file_edit_tool` | Force JSON patch format regardless of model |

```json
{
  "agent": {
    "edit_tool": "structured_patch"
  }
}
```

---

> [!NOTE] How it works internally
> See [Architecture Overview → Built-in tools](core-orchestration.md#built-in-tools).
