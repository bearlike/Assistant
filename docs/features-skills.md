# Skills

A skill is a small, self-contained instruction file that teaches the assistant a specific way of working. Examples include running code review, drafting a changelog, or triaging an incident. Each skill lives in its own directory as a `SKILL.md` file (YAML frontmatter plus a markdown body). Truss only pulls the full body of a skill into context when that skill actually activates, so you can keep dozens of skills installed without burning context on skills you aren't using.

> [!TIP] Drop-in compatible with Claude Code
> Skills follow the [Agent Skills standard](https://docs.claude.com/en/api/agent-skills) (also published as the open [`agentskills.io`](https://agentskills.io) spec). Truss uses the same directory conventions (`~/.claude/skills/` for user-global, `.claude/skills/` for project-local), the same `SKILL.md` frontmatter, the same `allowed-tools` scoping, and the same `/skill-name` invocation pattern. Any skill written for Claude Code works unchanged in Truss.

---

## Writing a skill

Create a directory at `.claude/skills/<your-skill-name>/SKILL.md`. The file must start with a YAML frontmatter block followed by the instruction body.

```markdown
---
name: code-reviewer
description: Review code changes for correctness, style, and test coverage. Use when asked to review a diff, PR, or commit.
allowed-tools: read_file aider_list_dir_tool aider_shell_tool
---

# Code Review

You are performing a thorough code review. Follow this checklist:

1. Read the diff using `read_file`.
2. Run `aider_shell_tool` with `git diff HEAD~1` to verify context.
3. Check for missing tests.
4. Report findings as a structured list: **Issue**, **Severity**, **Suggestion**.
```

### Frontmatter reference

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Lowercase, hyphens allowed, max 64 chars. Must match `^[a-z0-9]([a-z0-9\|-])*[a-z0-9]?$` |
| `description` | string | Yes | Used for auto-invocation matching (max 1024 chars) |
| `requires-capabilities` | string or list | No | Capability ids this skill needs; space-delimited string or YAML list. The skill is hidden from sessions that do not advertise all of them on `X-Truss-Capabilities`. See [Plugins & Marketplace → Capability gating](features-plugins.md#capability-gating). |
| `allowed-tools` | string or list | No | Tool IDs the skill scopes to; space-delimited string or YAML list |
| `disable-model-invocation` | boolean | No | Set `true` to hide this skill from the auto-invocation catalog (user `/skill-name` still works) |
| `user-invocable` | boolean | No | Set `false` to prevent explicit `/skill-name` invocation |
| `context` | string | No | Set `"fork"` to run the skill in a forked context |
| `agent` | string | No | Run skill inside a registered agent type |
| `model` | string | No | Model override when the skill activates |

---

## Tool scoping

When `allowed-tools` is set, activating the skill narrows the tool set for the duration of the skill to just those tools. Use the same tool IDs you would use anywhere else in Truss:

```yaml
allowed-tools: read_file aider_list_dir_tool
```

When `allowed-tools` is omitted, the skill inherits the full tool set of the current session.

---

## Capability gating

A skill can opt out of sessions that lack a required runtime by declaring `requires-capabilities` in its frontmatter. The client advertises capabilities via the `X-Truss-Capabilities` header; the orchestrator resolves the session capability set once, and `SkillRegistry` then filters both the auto-invocation catalog and the `/skill-name` lookup. Skills whose `requires-capabilities` is not a subset of the session capabilities are invisible — they are not surfaced to the model and cannot be invoked explicitly. The built-in `widget-builder` plugin uses this: its `st-widget-builder` skill declares `requires-capabilities: [stlite]`, so it never appears on CLI sessions that do not advertise stlite.

```yaml
---
name: st-widget-builder
description: Build an interactive stlite widget rendered inline in the console.
requires-capabilities: [stlite]
---
```

Skills inherit the capability list from their plugin when one is set — `plugin.json` can declare `requires-capabilities` at bundle level and have every skill under the plugin pick it up automatically. See [Plugins & Marketplace → Capability gating](features-plugins.md#capability-gating).

---

## Shell preprocessing

Skills can embed shell commands using the `` !`command` `` syntax. At activation time each matched command is executed and its standard output is substituted inline before the instructions are shown to the model. This is useful for injecting live context into the skill body. Examples include the current branch, directory tree, or environment values.

**Example.** Inject the current git branch name:

```markdown
You are reviewing code on branch: !`git rev-parse --abbrev-ref HEAD`
```

Commands time out after 30 seconds. On error, the placeholder is replaced with `[ERROR: ...]`.

---

## Where skills live

Skills are discovered from these directories. Project-local skills override personal skills with the same name.

| Path | Scope | Priority |
|---|---|---|
| `~/.claude/skills/<name>/SKILL.md` | User-global (all projects) | Lowest |
| `.claude/skills/<name>/SKILL.md` | Project-local (CWD) | Overrides personal |
| `<subdir>/.claude/skills/<name>/SKILL.md` | Subtree (nested inside the project) | Does not override above |

Plugins can ship skills too; they use the same `SKILL.md` format and never override a personal or project-local skill with the same name. A plugin's skills can declare `requires-capabilities` on the skill frontmatter, or inherit a bundle-level list from `plugin.json`.

---

## Invoking skills

You have two ways to trigger a skill:

- **Automatically**: the assistant reads the skill catalogue at the start of every session and can choose to activate a relevant skill based on your request.
- **Explicitly**: type `/skill-name` in the CLI or console. Arguments after the name are passed through as `$ARGUMENTS` inside the skill body, and individual tokens are available as `$0`, `$1`, and so on.

Sub-agents also see the skill catalogue in their system prompt, so delegating work to a sub-agent and naming a skill in the task description works as expected.

---

## Hot-reload

Truss notices when a `SKILL.md` file changes and picks up the new version automatically. New skill directories that appear while the server is running are detected on the next scan. No restart is required.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Skill loading](core-orchestration.md#skill-loading).
