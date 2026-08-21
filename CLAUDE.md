# Agents Guide — Mewbo

## MANDATORY: Hydrate before touching files

At the start of every conversation and every non-trivial task, ask a configured code-wiki MCP tool a question about `bearlike/Assistant` — try the primary configured one first, falling back to a secondary if one is configured. Use `read_wiki_structure` → `read_wiki_contents` for deeper exploration. Only read local files after hydration. Include this directive in subagent prompts.

> Wikis lag the repo. Use them for intuition; verify structure against local source.

## What Mewbo is

An AI assistant modelled as a conversation state machine with a hierarchical agent hypervisor. Core engine: a single async `ToolUseLoop` bound to an LLM via native `bind_tools`. Sub-agents admitted via `spawn_agent`, tracked by `AgentHypervisor`, resolved into one of four terminal states (`completed`, `failed`, `cancelled`, `rejected`). Interfaces: CLI, web console, REST API, Home Assistant, Nextcloud Talk — all share the core engine. For architecture details, query the wiki.

## Monorepo layering — read before adding a module

Dependencies flow **strictly down** this DAG; never up, not even a lazy `try/except ImportError`:

```
mewbo_core (lean SDK) → mewbo_tools · mewbo_graph · mewbo_iam → apps
```

- **`mewbo_core`** — lean orchestration SDK: generic, dependency-light primitives only. No product/graph code; heavy optional deps go behind a `mewbo-core[...]` extra.
- **`mewbo_tools`** — subprocess/remote integrations (MCP, LSP, file edit). Deps core only.
- **`mewbo_graph`** — optional capability library (graph, memory, embedding, SCG). Heavy deps behind extras + import-guards. Deps core only; never an app.
- **`mewbo_iam`** — optional identity kernel (principals, authenticators, roles, teams, grants, audit). Base deps are core + pydantic and NOTHING else; the network/crypto provider legs sit behind the `oidc`/`ldap`/`saml` extras. Deps core only; never an app.
- **apps** — thin product surfaces: HTTP routes, wire contracts, transport, glue. Compose libraries; never host a reusable engine.

**Placement rule.** Reusable engine → a library. Orchestration primitive → core. Subprocess/integration → tools. HTTP route/transport → an app. **(1) A reusable engine must never live inside an app. (2) Two apps must never import each other — extract the shared part into a library.**

**"Optional" means both layers:** PEP 621 extras AND a graceful `try/except ImportError` at every import site. Plugins/AgentDefs ship with the library whose substrate they wrap — `mewbo_graph.plugins.{wiki,scg}` are canonical. A library pushes its plugin root via `mewbo_core.plugins.register_builtin_root`; core never imports up to discover it.

**The rule has ONE standing violation, and it is debt.** `mewbo_core` reaches UP into `mewbo_tools` at 12 statically visible sites (`tooling/tool_registry.py` ×10, `loop/tool_use_loop.py`, `config.py`), all function-local; 11 are wrapped in `try/except Exception`, the `ToolSearchRunner` factory is not. A grep for `from mewbo_tools` UNDERCOUNTS — `tool_registry.py` also names `mewbo_tools.integration.*` as *strings* that `_import_factory()` and the manifest entries resolve through `importlib` at first use. It resolves at all only because the workspace installs both packages; core does not declare `mewbo-tools` as a dependency. **Nothing guards this**: `tests/test_core_import_graph.py` parses `mewbo_core.*` edges only. Do not add another — the cure is a registration seam pushed DOWN (`register_builtin_root` is the shape).

## Engineering principles

**KISS & DRY.** Bias toward less code. Search for an existing utility before writing anything custom.

Proven libraries: LiteLLM (LLM+embeddings), Pydantic (validation), Flask-RESTX (API), Rich/Textual (terminal), Langfuse (tracing), Jinja2 (prompts), Tiktoken (tokens), Loguru (logging), langchain-mcp-adapters (MCP), PyMongo (Mongo).

- **Strict Pydantic contracts at every trust boundary.** Anything crossing one — LLM tool arguments, HTTP request/response bodies, config files, persisted documents — is a Pydantic model with `ConfigDict(extra="forbid")`, validated AT DEFINITION (`field_validator` / `model_validator`). `extra="forbid"` is load-bearing, not hygiene: it turns a client smuggling a `token`, a `slug` rename, or a server-owned field into a clean 400 instead of a silent no-op (`wiki/settings.py:ProjectSettingsPatch`, `git_credentials_routes.py:CredentialUpsert`).
- **Behavior intrinsic to the data lives ON the model.** A family of variants is a **discriminated union** whose members own their validators + strategy methods — NEVER a service-side `if kind ==` switch, which drifts the moment a variant gains a field. `mewbo_core/triggers/spec.py` is canonical: five `TriggerSpec` kinds each owning its `next_fire_at`/`matches`/`verify`, one `Field(discriminator="kind")` parse seam, zero dispatch. **Models never import I/O** — the clock, a webhook's headers/body, a normalized CI/PR payload arrive as method ARGS, which is also what lets a test inject a fixed `NOW` instead of patching a clock.
- **One atomic class per feature: state + behavior together, collaborators by DI.** Clients, stores, policies, clocks and auth guards are injected as FIELDS — injecting the clock is what makes a watcher testable without sleeps. **No root-level module logic functions**: a helper belongs on the class owning the state it reads. Module-level CONSTANTS and thin factory aliases (`create_*_store`, `parse_trigger = TriggerSpec.parse`) are fine — data and ecosystem convention, not logic.
- **The Pydantic rule stops at the process boundary.** Hot in-process runtime state (`RunHandle`, `AgentHandle`, live loop bookkeeping) stays a plain dataclass — it crosses no trust boundary, so validating every mutation buys only hot-path cost.
- **Import-cycle cure = an EXISTING atomic home, never a new bare-function module.** Add the shared constant/helper as a member of the atomic class both modules already import. `ApiResponseKit` (`apps/mewbo_api/responses.py`) holds `TERMINATED_ERROR_BODY` + `terminated_response()` for this reason — an `errors.py` of loose functions re-opens the `backend.py` ↔ `triggers/routes.py` cycle it was meant to break.
- Smallest diff that solves the problem. No speculative abstractions.
- Tool contracts stable: `AbstractTool`, `ActionStep`, `TaskQueue`, `tool_id`/`operation`/`tool_input`.
- Tests prefer real code paths; stub only I/O boundaries.
- **An instruction describing how OTHER code behaves is a hypothesis — read that code before writing against it.** These failures are silent, not crashes: a renderer registered for an item kind nothing mints is dead code; a second implementation of an upsert rule is a divergence. Where a shared fixture or wire contract binds two surfaces, RUN it rather than reading one side — `tests/fixtures/transcript_timeline_corpus.json` is that seam, replayed by `tests/test_transcript_timeline_parity.py` AND `apps/mewbo_console/src/__tests__/timelineParity.test.ts`.
- **An empty grep is not absence.** Shell quoting eats `${...}`, a drifted cwd searches a different worktree, and the `grep` on PATH may be a drop-in that silently mangles a pattern it does not fully support — returning plausible matches for a pattern you did not write. Cross-check with a second spelling or a second tool.
- Cross-model tool-calling differences are **normalization concerns** — fix at the LiteLLM/adapter seam, never by detecting text format in the orchestration loop. See `packages/mewbo_core/CLAUDE.md` → "LLM client".
- **Every git subprocess goes through the hardened builders** (`mewbo_graph.plugins.wiki.clone` — the shared argv/env builders), and every credential resolves through the ONE chain in `mewbo_graph.wiki.credentials`. Iterating that chain is `run_git_with_chain`'s job; a caller handed a single resolved credential (`branches.py`) uses the hardened env without it. A hand-rolled `git` call re-opens the credential-helper trap that masks real auth errors. See `packages/mewbo_graph/CLAUDE.md` → "Git auth".
- Gitmoji + Conventional Commits (`✨ feat: ...`). See `.github/git-commit-instructions.md`.
- Never push unless explicitly asked. Treat LLMs as non-deterministic black-box APIs.

## Performance is a contract at every boundary

**The law: every function or endpoint that crosses a boundary declares what its cost
scales with, and no caller may depend on a tighter bound than the callee declares.**
Write the cost class in the docstring, in these terms:

| Class | Meaning | Where it is allowed |
|---|---|---|
| `O(1)` | bounded work regardless of stored data | anything on an interactive path |
| `O(one record)` | scales with ONE session / job / repo | detail reads, a single turn |
| `O(collection)` | scales with the NUMBER of records | listings — **and only with a bound** |
| `O(all history)` | scales with everything ever stored | **a defect on any request path.** Offline jobs only, and say so |

### The five rules

1. **A listing must never read its children.** Building N summaries by loading each
   record's full child collection is `O(all history)` in a listing's clothes: fine on a
   fresh install, degrading monotonically forever. Derive a summary field in the store
   with a projection, or maintain it on the parent — never by looping over children.
2. **A detail surface must never be gated on a collection query.** Fetch the one record;
   let the listing enrich when it arrives. Rule (1) seen from the client.
3. **A cursor or filter must narrow the WORK, not just the response.** A poll returning
   400 bytes that re-reads the whole record has a cursor in name only. Measure the time,
   not the payload — a shrinking response hides constant work.
4. **A filter that cannot be applied must refuse, never fall back to "everything."**
   Fail-open turns a client bug into a full-collection transfer that returns `200`, so
   nobody sees it. Constructively: where a read has no safe default scope, make the
   scoping parameter REQUIRED — a default of "everything" is the fail-open written once
   in the callee, while required turns every missed call site into a typecheck failure.
   `WikiStoreBase.query_graph`'s `scope: CommitScope` is the worked example.
   **Corollary — never give one parameter name opposite senses on two methods of the
   same class.** `count_graph_nodes(commit_sha=None)` means "stamped NULL, exactly"; a
   read using that name for "unscoped" makes every reader wrong about one of the two.
5. **Unbounded responses are a defect even when they are fast.** A response growing with
   a record's history needs a limit, window or cursor; server-side time ignores
   transfer, parse and render.

### Long-lived requests spend a process-wide budget

A streaming or blocking endpoint holds its slot for its whole life, not for the work it
does, so a handful can starve every other caller — the symptom is not "streams are slow",
it is "the entire API is down". State how many can exist at once and what happens beyond
that. **Refusing one stream is diagnosable; wedging the process is not.**

### Measurement law

**A performance claim is only closed by a measurement on the deployed artifact** — not a
passing test, not a landed commit, not reasoning about the diff. A suite cannot fail for
an event no client subscribes to, so a fix whose client half is missing passes everything.

- **Profile before you diagnose, and write the number down.** The obvious cause is
  routinely wrong; a single measurement refutes it.
- **Verify the artifact, not the source.** Grep the built bundle, hit the running
  endpoint, read the store. Source can be correct while what is deployed is not.
- **Never close a perf issue on local green.** If the measurement is not in the issue,
  the work is not done.

Per-surface budgets and profiling recipes live in each area's own `CLAUDE.md`.

## Issues are the unit of planning — not files

**A design, a plan, or a spec is an ISSUE. It is never a file in this repository.**
An issue carries a phased checklist ticked as work lands, comments that correct the
body, and a lifecycle that ends; a checked-in markdown plan has none of those and rots
silently. This covers any planning artefact a tool or skill offers to generate:
**take the analysis, file the issue, do not commit the document.**

**Every issue gets a Gitmoji title and a label set.** An unlabelled issue is invisible
to everyone who did not file it.

| Axis | Rule |
|---|---|
| `type/` | **exactly one.** The Gitmoji matches the one a closing commit would carry — `✨ feature`, `🐛 bug`, `♻️ refactor`, `⚡️ performance`, `🧹 chore`, `📝 docs`, `🧪 test`, `👷 ci`, `🔒 security`, `💥 breaking`, `🗑️ removal`, `🚧 spec`, `🔥 incident`, `🎨 design`, `⚙️ ops` |
| `Area/` | one, where the issue clearly belongs to a surface |
| `priority/` | one, where it is genuinely known — absent beats guessed |
| `status/` | zero or more, and only when specifically true: `🙋 needs-decision` (owner judgement, not engineering) · `🔍 needs-evidence` (a named claim is unverified) · `🚫 blocked` (external system this repo does not own) · `📐 spec-only` (implements nothing yet) · `🧊 parked` |

Pick `type/` by what a reader would search for, not by what happened first: a
security-relevant bug is `🔒 security`. An issue no commit can close is `⚙️ ops`.

**Writing the body:** state the symptom before the diagnosis (the diagnosis is
frequently wrong; the symptom survives being wrong). Cite `file.py:LINE` but say what
the code DOES — line numbers move and commit SHAs do not survive a squash merge, so a
reader verifies by content. Mark what you did not verify; an unmarked guess reads as a
measurement to the next reader.

**A closing keyword closes an issue but writes nothing.** When work lands, comment with
the corrected root cause and what was not verified, and tick the checklist to match
reality.

## Project instructions loading

`discover_all_instructions()` loads four levels (low→high): user `~/.claude/CLAUDE.md`, project `CLAUDE.md` / `.claude/CLAUDE.md` walking up to git root, rules `.claude/rules/*.md`, local `CLAUDE.local.md`. Subtree discovery walks DOWN from CWD (max depth 5) and indexes nested files for on-demand reading. Add `<!-- mewbo:noload -->` on line 1 to skip auto-loading a heavy file.

**`CLAUDE.local.md` (untracked, gitignored).** Holds machine- and environment-specific instructions that are not part of this repository. **If it exists at the repo root, read it before starting work** — it is the highest-precedence layer and overrides this file. Being gitignored it never arrives via clone or a new worktree, so a fresh checkout simply won't have one; nothing in it is required to build, test, or run Mewbo.

## CLAUDE.md tree

Read the deepest file that applies before editing. Every child carries `> ↑ parent · root` at the top.

| Scope | File |
|---|---|
| Engine: cross-cutting only — the three automata, package boundaries, capability gating, hooks, config curation | `packages/mewbo_core/CLAUDE.md` |
| Engine — zero-core-import contracts, bounded failure emission, diff arithmetic | `packages/mewbo_core/src/mewbo_core/contracts/CLAUDE.md` |
| Engine — LiteLLM client, the resilience ladder, the prompt registry | `packages/mewbo_core/src/mewbo_core/llm/CLAUDE.md` |
| Engine — admission, the agent lifecycle, delegation, Communication Units | `packages/mewbo_core/src/mewbo_core/agents/CLAUDE.md` |
| Engine — the SessionTool declaration law, tool scoping, deferred schemas, ask-user | `packages/mewbo_core/src/mewbo_core/tooling/CLAUDE.md` |
| Engine — the durable session record, compaction, origin + trace provenance | `packages/mewbo_core/src/mewbo_core/session/CLAUDE.md` |
| Engine — the turn automaton, the completion seam, binding, session runtime | `packages/mewbo_core/src/mewbo_core/loop/CLAUDE.md` |
| Engine — name→directory resolution, the repository registry, containment | `packages/mewbo_core/src/mewbo_core/workspaces/CLAUDE.md` |
| Engine — API key storage and the store-composition pattern | `packages/mewbo_core/src/mewbo_core/secrets/CLAUDE.md` |
| Engine — durable reverse-invocation triggers | `packages/mewbo_core/src/mewbo_core/triggers/CLAUDE.md` |
| Engine — operator-authored system instructions | `packages/mewbo_core/src/mewbo_core/system_instructions/CLAUDE.md` |
| Engine — first-party plugin suites in the core wheel | `packages/mewbo_core/src/mewbo_core/builtin_plugins/CLAUDE.md` |
| Integrations: MCP pool, file edit, LSP, Aider | `packages/mewbo_tools/CLAUDE.md` |
| Graph/memory/embedding/SCG substrate | `packages/mewbo_graph/CLAUDE.md` |
| MewboWiki indexing job lifecycle (engine: phases, resume, progress) | `packages/mewbo_graph/src/mewbo_graph/wiki/CLAUDE.md` |
| SCG plugin tools (map + search) | `packages/mewbo_graph/src/mewbo_graph/plugins/scg/CLAUDE.md` |
| Identity kernel: principals, authenticators, roles, teams, grants, audit | `packages/mewbo_iam/CLAUDE.md` |
| Speech: gateway-backed synthesis and transcription, model capability discovery | `packages/mewbo_speech/CLAUDE.md` |
| HTTP API server (routes, channels, Web IDE) | `apps/mewbo_api/CLAUDE.md` |
| MewboWiki — API side | `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` |
| Agentic Search — API side | `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` |
| Agentic Search — SCG lifecycle glue | `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md` |
| Mewbo Apps — API side (stores, lifecycle, routes, SDK injection) | `apps/mewbo_api/src/mewbo_api/apps/CLAUDE.md` |
| IDE broker: the Node service owning the docker socket + per-session `code-server` | `apps/mewbo_ide/CLAUDE.md` |
| Web console (React, shadcn, TanStack Query) | `apps/mewbo_console/CLAUDE.md` *(noload — heavy)* |
| Console NavRail — the single navigation surface | `apps/mewbo_console/src/components/nav-rail/CLAUDE.md` |
| MewboWiki — Console side | `apps/mewbo_console/src/components/wiki/CLAUDE.md` |
| Agentic Search — Console side | `apps/mewbo_console/src/components/agentic_search/CLAUDE.md` |
| Mewbo Apps — Console side (gallery/detail, stlite app panel) | `apps/mewbo_console/src/components/apps/CLAUDE.md` |
| MCP server: tools exposing Mewbo to agents | `apps/mewbo_mcp/CLAUDE.md` |
| CLI (Rich/Textual display, agent panel) | `apps/mewbo_cli/CLAUDE.md` |
| Aura Android client (Compose, orb overlay, redroid dev loop) — indexes its own per-package children | `apps/mewbo_aura/CLAUDE.md` |
| Home Assistant conversation agent | `apps/mewbo_ha_conversation/CLAUDE.md` |
| Demo-as-code: seeded demo stack + artifact rendering | `demo/CLAUDE.md` |
| Demo framer: window-on-wallpaper compositor, source-to-derived doc images | `demo/framer/CLAUDE.md` |
| Test patterns + fixtures | `tests/CLAUDE.md` |
| Docs site: code-ref badges, Scalar, authoring | `docs/CLAUDE.md` |

## MCP tools — when to use each

- **Code-wiki MCP tools** (`ask_question`, `read_wiki_structure`, `read_wiki_contents`) — primary context source. Try the primary configured wiki tool first, fall back to a secondary if configured. Handles up to 10 repos at once.
- **Remote coding-agent session tools** (`*_session_create`, `*_session_interact`, etc.) — delegate long-running tasks, manage knowledge notes, schedule automated work. Session IDs need a provider-specific prefix when reused.
- **Langfuse** (`mcp__langfuse__*`) — observability. Path: `get_error_count(age)` → `fetch_sessions` → `fetch_traces` → `fetch_trace(id, include_observations=true)` → `fetch_observation(id)`. Filter by `sessionId` == Mewbo session_id. `mewbo-tool-use` arrives as a trace TAG (trace name is `step:N`); `mewbo-task-master`/`mewbo-context` are `user_id` values, not trace names. `age` in minutes (max 10080). Use `output_mode="full_json_file"` for large payloads.
- **SearXNG** (`searxng_web_search`) + `web_url_read` — current events, docs, errors outside codebase.
- **Context7** (`resolve_library_id` → `query_docs`) — library/framework API docs.

Fire MCP calls in parallel. The code-wiki tool = "how should it work"; Langfuse = "how did it actually work."

## Debugging sessions

Full methodology: `apps/mewbo_api/CLAUDE.md` → "Debugging session errors". Orientation:

- **Mongo is authoritative for sequence** — `db.events.find({session_id}).sort({ts:1})`.
- **Mongo is also the better attribution source.** `llm_call_start`/`llm_call_end` carry `agent_id`/`depth`/`step` plus per-call `input_tokens`/`output_tokens`/cache counts, exactly equal to Langfuse's `promptTokens`/`completionTokens`. Langfuse has no depth axis and its span tree orphans parents under concurrent sub-agents — **join the two by session, never by span.**
- **Cost/cache truth is the LiteLLM proxy's Postgres spend log** (`LiteLLM_SpendLogs.metadata->usage_object` → `cache_creation/read_input_tokens`). Langfuse's cost column reads literally zero for any model string lacking a Langfuse Model Definition, which a proxy-prefixed name routinely does; its `cache_hit` column tracks the Redis response cache, NOT Anthropic prompt caching.

## Running, testing, linting

- Tests: **`.venv/bin/python -m pytest` from the repo root** — the canonical invocation, honouring every entry in `testpaths` (`tests/`, the three app suites, both demo suites). Two ways to get a meaningless green, and both look like a passing run:
  - **`pytest` bare may not be this project's pytest.** Where a version manager puts a shim first on `PATH`, it loads the wrong plugin set, dies with `Marks cannot be applied to fixtures` — and **exits 0 having collected NOTHING**. A zero exit that proves nothing is worse than a failure. Confirm the count: a real run collects ~11,500 tests, so a summary naming far fewer, or none, is the shim rather than a clean tree.
  - **Naming a path OVERRIDES `testpaths`**, so `pytest tests/` runs one subtree and reports green for suites it never collected; that is how an entire app suite stayed out of the usual run. Pass a path only when you mean to narrow.
- **⚠️ A bare `uv sync` STRIPS the shared `.venv` down to the lean install — 97 packages, pytest's plugins, ruff and mypy among them.** `[tool.uv] default-groups = []` means a sync with no flags installs no dependency group at all, and **`uv sync` is EXACT by default**, so "not requested" reads as "remove". That leanness is intentional and stays: `uv sync --extra api` is the published quick start and must not drag a dev toolchain in. Re-sync with the full `uv sync --all-extras --all-groups`, never a narrower form, on a checkout anyone else is using.
  - **`uv run` does NOT do this, and the difference is a default, not a detail.** `uv run` is INEXACT by default — it installs what is missing and removes nothing — which is why `uv run --exact` exists as an opt-in and `uv sync --inexact` as the opposite opt-out. Each flag's existence is the proof of the other's default. Verified against a throwaway workspace built in this project's shape: a bare `uv run`, and `uv run --package <member>`, both leave the dev group and every extra in place, while a bare `uv sync` in the same fixture removes them. So `uv run …` is safe to use; `uv run --exact` is the one to never type here.
  - **Do not "fix" this by putting `dev` back in `default-groups`.** It would break the published lean install, and it would not even work: uv has `default-groups` but no `default-extras`, so a lean sync still drops `mewbo_graph` and the tree-sitter stack, `mewbo_mcp` and `mewbo_ha_conversation`, taking whole test subtrees with them.
- Install: `uv sync` (core) or `uv sync --all-extras --all-groups` (dev). The dev form is what the shared `.venv` is built from, so any narrower sync run against it is a downgrade, not a no-op.
- Run: `uv run mewbo` / `uv run mewbo-api` from repo root, or `npm run dev` in `apps/mewbo_console`.
- Config chain: `CWD/configs/` → `$MEWBO_HOME/` → `~/.mewbo/`. `$MEWBO_CONFIG_DIR` pins the directory ahead of the CWD walk — the walk itself can't be redirected for a spawned child, since it re-runs from that child's own CWD. Override with `--config`. Run `/init` to scaffold.
- Lint: `ruff check .` (auto-fix: `ruff check --fix .`). Types: `mypy`. Helpers: `make lint`, `make lint-fix`, `make typecheck`, `make precommit-install`.
- **Never blind `ruff --fix`** — always re-run `ruff check .` after any autofix (strips intentional `noqa`).
- **`.devcontainer/` is a compose stack with its OWN docker daemon and its OWN Mongo** — see `.devcontainer/README.md`. The point is that an agent can do full-stack work without host line of sight. Its Mongo volume (`assistant_devcontainer_mongo-data`) is deliberately distinct from the production `assistant_mongo-data`.
- **`configs/app.json` is gitignored, so a fresh checkout or worktree falls back to `configs/app.example.json` — which ships an EMPTY `llm.api_base`.** The stack then comes up healthy and still cannot reach the model gateway; the network was never the missing piece. Any tooling that provisions a new worktree must seed the whole gitignored config layer (`CLAUDE.local.md`, `.mcp.json`, `configs/app.json`, `.env`) — and `.env` must be seeded FILTERED, because it carries both the stack's values and the `MEWBO_*_SRC` host source-mount pointers, which name absolute paths in the ORIGINAL checkout. Copied verbatim they mount the wrong tree's source into the new container, which then silently runs code you are not editing.
- **Working in a worktree: pass `git -C <worktree>` on EVERY git call, never a bare one.** A shell's cwd can drift back to the main checkout between commands — silently, with no error and nothing in the output to notice — so a bare `git pull --rebase origin <branch>` typed for a worktree can execute in the main checkout while standing on `main`. It fast-forwards `main` onto the feature branch, and the next push publishes the whole branch with no pull request and no verification run. Nothing warns you: every command succeeds. The cure is positional, not procedural — name the directory in the command and the drift cannot reach it.
- **Commit with `--no-verify` whenever anything else shares the checkout** (a second agent, a second terminal, a parallel worktree session). `pre-commit` stashes *every* unstaged file in the repo, not just the paths you staged, so it holds the other worker's in-flight edits for the duration — and the repo-wide `mypy` hook is slow enough that a timeout or a kill leaves them stashed with the tree looking clean. Recovery: the stash is a plain patch under `~/.cache/pre-commit/patch*` (newest = yours) — `git apply` it back. Run `ruff check .` and `mypy <touched files>` by hand: same gate, scoped to what you changed.
