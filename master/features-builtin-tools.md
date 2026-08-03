# Built-in Tools

Mewbo ships a set of first-party tools that every session has available by default. These tools cover the full local-development surface. You get reading files, editing them, running shell commands, and browsing directory trees. Because they are bundled with the core, no MCP server or external process is required. They activate the moment a session starts.

This page documents every built-in tool, its parameters, example output, and the configuration switches that control which editing backend is active.

For setup and installation, see [Getting Started](getting-started.md). For tool permissions and approval modes, see [The Interface](terminal/interface.md) page of the terminal client.

## Tool catalog

| Tool ID | Name | Read-only | Description |
|---|---|---|---|
| `read_file` | Read File | Yes | Line-windowed file reader |
| `aider_list_dir_tool` | List Directory | Yes | Recursive directory listing |
| `aider_shell_tool` | Shell | No | Run arbitrary shell commands, foreground or background |
| `shell_session_tool` | Shell Session | No | Read output from, write input to, or stop a background shell |
| `aider_edit_block_tool` | Aider Edit Blocks | No | Apply Aider-style `SEARCH/REPLACE` blocks to files |
| `file_edit_tool` | File Edit | No | Exact string replacement with `old_string` / `new_string` |
| `home_assistant_tool` | Home Assistant | No | Smart home control (enabled when Home Assistant is configured) |
| `lsp_tool` | Language Server | Yes | Code diagnostics, go-to-definition, references, hover |

Alongside those, a session binds tools whose lifecycle is tied to the session rather than the registry: `ask_user_question`, `list_projects`, and `switch_project` are documented below and appear only under the conditions each section names.

---

## read_file

`read_file` reads a local file and returns its content with 1-based line numbers, mirroring `cat -n` output. Reads are line-windowed: the default window is 2 000 lines, and `offset` / `limit` let the session page through arbitrarily large files without exhausting the context window. Mewbo also deduplicates repeated reads of the same slice so the same content does not consume context twice. In the web console, every `read_file` call renders as an expandable card in the session timeline showing the project, file path, and the exact line window the assistant pulled into context.

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-console-file-read-log.jpg" alt="A read_file tool card in the Mewbo console showing lines 81 through 90 of a 733-line litellm-config.yml with a truncated tag" style="width: 100%; max-width: 720px; height: auto;" />
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
  <img src="../assets/img/mewbo-console-04-file-edit.jpg" alt="A file-edit tool card in the Mewbo console showing a unified diff with +23 additions and -2 deletions" style="width: 100%; max-width: 720px; height: auto;" />
</div>

The active backend for a session is chosen automatically based on the model, or you can pin it via [`agent.edit_tool`](configuration.md#agent) in [`configs/app.json`](repo:configs/app.example.json).

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
  <img src="../assets/img/mewbo-console-shell-log.jpg" alt="A shell tool card in the Mewbo console showing a gh release list command with its JSON response and a 348ms duration" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string | Yes | Shell command to execute |
| `cwd` | string | No | Working directory (defaults to `root`, then CWD) |
| `root` | string | No | Project root used for safe-path resolution |
| `timeout` | number | No | Seconds before a foreground command is killed (default 115) |
| `run_in_background` | boolean | No | Return a `shell_id` immediately instead of waiting |
| `tty` | boolean | No | Allocate a pseudo-terminal so the command can be driven interactively |

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
- Shell invocations require approval in the default permission policy. See [The Interface](terminal/interface.md) for approval modes and auto-approve flags.
- A foreground command gets no writable stdin, so anything reading stdin (a pager, a credential prompt) returns immediately instead of blocking until the timeout.
- With `run_in_background`, the response carries a `shell_id` in place of `exit_code`/`duration_ms`, and the command keeps running in its own process group after the call returns.

### Filesystem scope

The `cwd` check above selects where a command starts and nothing more. A shell command is an opaque string, so `cat`, `grep`, or a Python one-liner can read straight out of the working directory it was given. Mewbo therefore confines each shell subprocess with the Linux kernel's Landlock LSM, controlled by [`agent.shell_sandbox`](configuration.md#agent) and on by default. It is a deny-list: every configured project other than the session's active one is denied, together with anything listed in `agent.shell_denied_paths`. Everything else — the interpreter, system libraries, the toolbox on `PATH`, the home directory — stays reachable, so nothing has to be enumerated to keep ordinary commands working.

To let a project's sessions reach a directory that would otherwise be denied, such as a sibling checkout or a shared data directory, list it under `allowed_paths` on that project:

```json
{
  "projects": {
    "api": {
      "path": "/srv/projects/api",
      "allowed_paths": ["/srv/projects/shared-protos"]
    }
  }
}
```

Three limits, stated plainly. This confines the shell tool only; every other built-in tool validates its path arguments instead. Landlock only ever removes access, so ordinary filesystem permissions still apply underneath. And on a kernel without Landlock support it logs one line and changes nothing.

---

## shell_session_tool

`shell_session_tool` observes and steers a command started with `run_in_background`. It is what makes backgrounding usable: without a way to read output, answer a prompt, and stop the process, a background start is a process nothing can reach.

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `operation` | string | Yes | `read`, `write`, `kill`, or `list` |
| `shell_id` | string | For all but `list` | Handle returned by `run_in_background` |
| `input` | string | No | Text sent to stdin (`write`) |
| `newline` | boolean | No | Append a newline to `input` (default true) |
| `cursor` | integer | No | Return only output produced after this cursor |
| `filter` | string | No | Regular expression; only matching lines are shown |
| `wait_ms` | integer | No | Wait up to this many ms for new output (max 30000) |

### Example output

```json
{
  "shell_id": "shell_1",
  "command": "npm run dev",
  "status": "running",
  "exit_code": null,
  "output": "  ➜  Local:   http://localhost:5173/\n",
  "cursor": 812
}
```

### Behavior notes

- **Reads are incremental.** Pass back the `cursor` from the previous read to get only what arrived since; omit it to get everything retained.
- **`status` comes from the process, not from its output.** A buffered command can print nothing for its whole run, so an empty read never means "finished". Check `status` and `exit_code`.
- **`filter` is display-only** and never consumes output — a filtered read leaves the cursor exactly where an unfiltered one would.
- **`missed_characters`** on a read means output was evicted from the buffer before it was read. It is unrecoverable; read more often or narrow the command's own output.
- **`write` reads the reply back in the same call**, so answering an interactive prompt costs one step rather than two. Writing to an exited session is refused rather than silently discarded.
- **`kill` terminates the whole process group** (SIGTERM, then SIGKILL after a grace period), so children die with the command.
- **Sessions are capped.** Finished sessions are evicted to make room; when every slot holds a running command, a new background start is refused with a message naming the cap. Idle sessions are reaped automatically.

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

## ask_user_question

`ask_user_question` lets the assistant put a decision back to you instead of guessing at it. It asks one to four related questions as a single card in the session timeline, each with two to four options or as free text, and a free-text answer is always accepted even when options are offered. By default the run blocks until you answer, so nothing happens on a wrong assumption while you are away. A call may instead name its own `timeout_seconds`, and expiry is then a readable result the assistant acts on rather than a failure: it proceeds on its stated assumption or stops and reports that it is waiting on you.

A question stays answerable until it is answered. A timeout, a newer message, or even an API restart does not close it, so a card whose run stopped waiting still takes your answer, which then arrives in the session as a new message. The card below shows all three states at once: a group whose run timed out and remains open, a multi-select group with the optional notes box, and a group still waiting inside its bounded window. The tool is available only when the client can actually ask you, so headless drives such as triggers and channels never bind it, and only the root agent can use it. A sub-agent reports its open questions back through its result.

<div style="display: flex; justify-content: center;">
  <img src="../assets/img/mewbo-console-ask-user-log.jpg" alt="Three ask-user question cards in the Mewbo console: a timed-out card badged Still open that says a late answer arrives as a new message, a multi-select card with a notes box, and a card badged Awaiting your answer that waits up to 30m" style="width: 100%; max-width: 720px; height: auto;" />
</div>

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `questions` | array | Yes | One to four questions, each with a short `header`, the `question` text, optional `options` (empty, or two to four `{label, description}` choices), and `multi_select` |
| `timeout_seconds` | integer | No | Seconds to wait before the call resolves as timed out; omit to wait indefinitely |
| `notes_placeholder` | string | No | Hint text for an optional free-text notes box shown alongside the questions |

### Behavior notes

- The answer returns as an ordinary tool result, so the assistant keeps working in the same run. There is no default-answer concept and no timeout policy switch.
- Sending a new message while a question is pending supersedes it. The message is addressed instead, and the question remains answerable.
- Answers are one or more selected option indexes or free text for each question, never both, plus the optional notes for the group as a whole.

---

## list_projects

`list_projects` enumerates every project the running session could move into: directories an operator registered by hand, projects and worktrees Mewbo manages itself, and git repositories that have been registered with Mewbo. Each entry reports a key, a name, its kind (`configured`, `managed`, `worktree`, or `repository`), a description, whether it is currently available on disk, and its repository slug and branch when known. A repository that has been registered but never checked out still appears in the list, so the model can see that it exists, but there is nothing to work on there until a checkout exists.

The tool takes no parameters and returns the full list in one call. It exists only when the session is running in [auto workspace mode](project-configuration.md#choosing-a-workspace), and only for the root agent.

---

## switch_project

`switch_project` moves the running session into one of the projects `list_projects` reported. The working directory changes, the target project's `CLAUDE.md`/`AGENTS.md` instructions are loaded the same way described in [Project Configuration](project-configuration.md#instruction-file-loading), and any sub-agent spawned after the call inherits the new directory. A sub-agent already running when the switch happens keeps the directory it started in.

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | Yes | The key of a project reported by `list_projects` |

### Behavior notes

- Switching into a key that does not exist, or a registered repository with no checkout, is refused rather than silently falling back to the previous directory.
- The call can be made repeatedly. A task that genuinely spans more than one project switches back and forth as needed.
- A switch never grants more tools than the run started with. The tool registry is rebuilt for the new directory, but the bound set narrows to what the agent already held, so a project's own `.mcp.json` servers are not admitted partway through a run. A fresh session against that project resolves them normally. Skills accumulate instead of being replaced, so plugin-contributed and user-level skills survive a switch.
- Like `list_projects`, this tool exists only in auto workspace mode and only for the root agent. A sub-agent works in whatever directory it was spawned into and cannot re-scope the whole session; a parent can hand a sub-agent its own project at spawn time instead.

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
