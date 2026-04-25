# Git Commit + PR Instructions (Truss)

Use this file for **every** commit and PR title/body in this repo. It codifies our Gitmoji + Conventional Commits rules and common pitfalls to avoid.

## Required format (Gitmoji + Conventional Commits)
```
<gitmoji> <type>(<scope>): <description>

<body>

<footer>
```

### Type (Conventional Commits)
Use one of: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
`build`, `ci`, `chore`, `revert`.

### Scope (required here)
Use a **real module/component** name. Examples:
`orchestration`, `core`, `tools`, `mcp`, `permissions`, `cli`, `api`, `chat`,
`ha`, `prompts`, `docs`, `ci`, `build`, `tests`.

Avoid random scopes (e.g. `release`, `misc`, `tmp`).

### Description
Imperative, short, and specific (e.g., "stabilize MCP tool inputs").

### Breaking changes
Use `!` after the type/scope and include a `BREAKING CHANGE:` footer entry.

## Gitmoji mapping (use the right one)
- ✨ feat: new feature
- 🐛 fix: bug fix
- ⚡️ perf: performance improvement
- ♻️ refactor: refactor without behavior change
- 🧪 test: tests only
- 📝 docs: documentation
- 👷 ci: CI changes
- 🏗️ build: build system/deps
- 🔧 chore: misc non-src/non-test changes
- ⏪️ revert: revert
- 💄 style: formatting only

Use unicode emoji (not `:shortcode:`) for commit titles.

Avoid overusing ✨. Use it **only** for new user-facing features.

## Example titles (use this exact format)
- 🧪 chore(ci): avoid openai in orchestration test
- 🐛 fix(orchestration): stabilize MCP tool inputs
- 📝 docs(readme): refresh badges and setup links

## Commit body (required)
Always include a body with:
1. **What changed** (bulleted list).
2. **Why** (brief rationale).
3. **Tests run** (or "Not run" + reason).
4. **Dependencies / env changes** (if any).

Example body:
```
- Add schema-aware MCP input coercion and update tool manifest.
- Fix tool response synthesis step to keep user-facing output clean.

Tests: .venv/bin/pytest tests/test_orchestration.py -q
```

## PR titles and bodies
PR titles must use the same `<gitmoji> <type>(scope): description` format.
PR bodies must list:
- Summary of changes
- Tests run
- Notes/risks (if any)

## Common pitfalls (avoid)
- Wrong Gitmoji (e.g., using ✨ for docs/ci/refactor).
- Missing/incorrect scope.
- Empty body or no tests listed.
- Overly broad or vague description.
