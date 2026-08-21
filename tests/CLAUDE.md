> ↑ [root /CLAUDE.md](../CLAUDE.md)

# Tests — Project Guidance

Scope: the root `tests/` suite and shared test patterns.

## Where the suites live

Each file's own docstrings list what it covers; only the non-obvious seams are
recorded here.

| Suite | The seam it pins |
|---|---|
| `test_tool_use_loop.py` | The loop runs to natural completion — there is no `max_steps`. Drives `asyncio.run()` + `AgentContext.root()` + `AsyncMock` on `model.ainvoke`. |
| `test_agent_context.py` | Hypervisor lifecycle, admission, budgets, stall detection. `send_message`/`cancel_agent`/`send_to_parent` return `str \| None` — None means SUCCESS, a str is the diagnostic failure reason. |
| `test_spawn_agent.py` | Tool filtering goes through `filter_specs()` in `tool_registry`, so mock `mewbo_core.tooling.tool_registry.get_config_value` — NOT `spawn_agent.get_config_value` — when testing config-denied tools. |
| `test_user_turn_persistence.py` | The accepted turn is recorded exactly once. `start_async` writes the `user` event before the executor's cold start, asserted through explicit gates and never sleeps (a timing-based version passes regardless of which seam wrote it); a refused start writes neither `run_accepted` nor `user`; the default `user_turn_persisted=False` keeps a direct `Orchestrator.run` writing its own. The cardinality test drives the REAL `start_async → run_sync → orchestrate_session → Orchestrator.arun` chain, stubbing only `ToolUseLoop.run`, so a flag dropped at any hand-off surfaces as a duplicate turn. |
| `test_package_imports.py` | Every module of all eight shipped packages, one fresh interpreter per package, probed concurrently (~20s). Discovery excludes only what has no dotted name — a `.py` file whose directory has no `__init__.py` — and that excluded set is pinned EXACTLY, so an `__init__.py` vanishing from a real package directory fails instead of silently dropping every module beneath it out of the sweep. |
| `test_packaged_resource_anchors.py` | The sibling defect an import gate cannot see: a packaged asset resolved from a module's own `__file__` instead of from the package that owns it. |
| `test_hooks_http.py` | Mocks `_post_json` to avoid real HTTP; covers session env enrichment (`MEWBO_SESSION_ID`, `MEWBO_ERROR`). |
| `test_channels.py`, `test_email_channel.py` | Adapter protocol compliance, HMAC verification, MIME/threading parsing, mention gating — all offline. |
| `apps/mewbo_api/tests/test_vcs_pickup.py` | Stub by patching `vcs_pickup._service` attributes; **three** real-git tests exercise fetch→worktree→ff against a bare origin + clone. They share a function-scoped `cloned_project` fixture, so that ~11-subprocess git setup is paid once per test, not once per file. |
| `test_github_workflows.py` | Workflow contract for `.github/workflows/agent-pickup.yml`, incl. injection safety: event data reaches `run:` only via `env:`, never inline `${{ github.event.* }}`. |

Also covered: tool registry disabling on init failure, MCP discovery (schema
normalization, per-server failures, CLI visibility when tools are missing),
plugin discovery/install/hook translation/session-init integration, the agent
definition registry, session runtime (fork-from-tag, `fork_at_ts`, archiving),
instruction discovery, orchestration flow, and CLI display + integration.

## Assumptions

- Many tests rely on `monkeypatch` for env vars (LLM config, MCP config, log levels).
- LLM calls are mocked at the orchestration boundary (`orchestrate_session`) or via `AsyncMock` on `model.ainvoke`.
- `ToolUseLoop` tests require `AgentContext.root()` with an `AgentHypervisor`.
- `ToolUseLoop.run()` is async — wrap calls with `asyncio.run()` in sync test methods.
- Agent lifecycle hooks (`on_agent_start`/`on_agent_stop`) fire from async context — use thread-safe mocks.
- Never pull in real MCP servers or external HTTP.

## Pitfalls

- **`caplog` captures NOTHING from these modules — they log through loguru.** `caplog` hooks stdlib `logging`; loguru is a separate sink chain, so a test asserting on a warning via `caplog` passes vacuously (empty records — and an `assert "x" not in caplog.text` check is worse, it can never fail). Add a temporary loguru sink appending to a list for the duration of the assertion, which is loguru's own documented test pattern. Applies to every module using `common.get_logger`.
- **Never run two pytest invocations concurrently** — suites share file-backed stores (session/project store under the same data root), so parallel runs cross-contaminate and produce one-off phantom failures that don't reproduce.
- **Patching `<module>.<stdlib>.<attr>` patches it for the whole PROCESS, not for the module you named.** `some_module.time` IS the stdlib `time` module — the same object every other module and every thread holds — so `monkeypatch.setattr("pkg.mod.time.sleep", recorder)` reaches all of them. A recorder installed that way collected over a million entries from a background poller and turned one assertion red under full-suite load while passing in isolation; the list was not wrong about what it saw, it was seeing the entire process. The cure is the DI rule this repo already states, applied to time: give the production class an injected `sleeper`/clock FIELD and hand the test its own. Patch `pkg.mod.sleep` (a name the module owns) if you must, never `pkg.mod.<stdlib module>.<attr>`.
- **Before diagnosing an ordering failure, confirm the file that RAN is the file at HEAD.** This checkout hosts sibling git worktrees under `.worktrees/` and `.claude/worktrees/`, both gitignored, and `testpaths` excludes them — so a worktree is invisible to `git status` AND to collection, and an agent working in one that writes into the MAIN checkout's `tests/` leaves no trace either place. **Get the failing test NAMES and grep for them at HEAD first**: a name that exists nowhere at HEAD means the run collected another session's in-flight file, not that you have a regression. The failure COUNT misleads here — such failures partition along plausible-looking seams unrelated to the cause.
- **Full-suite ordering failures = global-state leak** (once the above is ruled out). A test that passes in isolation but fails under full-suite ordering has a leaked singleton (namespace, event loop, config). Route tests must NOT re-register a Flask-RESTX namespace on the shared backend app — `add_url_rule` after the app handled its first request raises (Flask setup frozen); rely on `backend.py`'s import-time wiring. Async unit tests use `asyncio.run` (fresh loop per call), never a shared `get_event_loop()` a prior test may have closed.
- **Assert loudly on the match, not just on the derived result.** A parser or classifier built on a regex degrades silently the moment the upstream string format changes: `if matches:` is a guard, not an assertion, so a test written the same way falls through the `else` branch and still passes against a now-unmatched input. Where a fixture is known to match, assert `matches` itself.
- **Anchor a fixture by a stable identifier, never by position.** A helper that reaches for "the last event of this type" or "the first matching row" is one new record away from reaching for the wrong one. Filter on a stable id/name carried IN the record; position is an accident of write order, not a contract.
- **A model that DERIVES its own id discards the one your fixture wrote — and a test bound to that literal passes vacuously.** `Entity.id` (`sha1(normalized_name|type)`) and `MemoryNode.node_id` (`compute_node_id`) are both recomputed at validation, so `Entity(id="e1", …)` stores a hash and an edge written against `"e1"` matches NOTHING. The dangerous half is the direction that still passes: an assertion of the form "this anchor is DROPPED" (`== ()`, `is None`, `not in`) is satisfied just as well by the edge never being collected at all, so it goes green while testing nothing. **Bind fixtures to the id the model actually minted (`ent.id`, `note.node_id`), and always pair an it-is-dropped assertion with an it-resolves case over the same seam** — the positive is what proves the negative is not vacuous.
- **An expectation must be spelled the way the STORE spells it, and the way to guarantee that is to call the store's own normaliser.** Timestamps are the recurring case: `EventCursor.canonical` deliberately always writes microseconds (a bare `isoformat()` drops a zero-microsecond field, and every `ts` comparison in the store is TEXTUAL, so the two spellings sort differently). Re-spelling the format in the test just moves the drift one file over — call the normaliser.
- **Reproduce, don't describe.** A claim built from reading a prompt, a truncated grep result, or an editor's rendered overlay is a hypothesis, not a finding. A reproduction case must construct the shape under test BY HAND where necessary: a fixture built by current code can never reach a path that only a legacy record takes, since current code no longer writes that shape.
- **Assert against the runtime the ROUTE holds, not `backend.runtime`.** `test_backend._reset_backend` rebinds `backend.runtime`/`backend.session_store` by **plain assignment** (no monkeypatch restore) → it leaks for the rest of the suite. A namespace captured its runtime at `init_*` time (e.g. `realtime.routes._runtime`), so the session a route persisted lives THERE, not in whatever `backend.runtime` now points at. Read the route module's own `_runtime` to verify a persisted side-effect. Corollary for write-behind routes: force the persist synchronous (patch `persist_async`→`persist`) so the daemon-thread write has landed before the assert.
- **Config overrides do NOT leak between modules, and the count of modules that "forget"
  `reset_config` is not evidence that they do.** The root `conftest.py`'s autouse
  `app_config_file` calls `reset_config()` at BOTH the setup and the teardown of every test, and
  `reset_config` clears the override mapping outright — so the eleven modules that call
  `set_config_override` without resetting are harmless. Probed, not reasoned: each suspect module
  run immediately before an assertion that the override set is empty, all clean. The related
  worry that a function-scoped autouse reset would clobber a module- or session-scoped config
  fixture does not apply either — that reset already exists, and no module- or session-scoped
  fixture in the suite touches config at all. `tests/test_config_isolation.py` pins both halves,
  because the day the autouse fixture loses one of them the symptom lands in the shell sandbox's
  deny set, in a module that did nothing wrong.
- **A suite-level env pin must ASSIGN, never `setdefault`.** The `MEWBO_CONFIG_DIR` pin exists so
  the config chain cannot walk up to a developer's live, secret-bearing `configs/app.json`.
  Written with `setdefault` it deferred to an inherited value — inert in exactly the environment
  it was written to defend against, and a run reading a real deployment's config is
  indistinguishable from a healthy one. It must be an env var and not only an in-process override,
  because much of this suite probes behaviour in SUBPROCESSES and a child re-runs the chain from
  its own CWD; the env var is the only form of the redirect a child inherits.
- Over-mocking hides real behavior. Mock only the LLM call boundary and tool execution boundary.
- Exercise schema mismatches (string tool_input, dict tool_input, invalid schema, required fields). Missing-tool tests assert `last_error` and follow the production path.
- Keep tests deterministic: fixed timestamps, fixed session IDs, explicit env.

## Preferred patterns

- Fake tools implementing the same `ToolSpec` interface.
- `_make_agent_context()` (in `test_tool_use_loop.py`) for root contexts.
- `AsyncMock` for `model.ainvoke` — `return_value` or `side_effect` for multi-step conversations; `_text_response()` / `_tool_call_response()` to build mock `AIMessage` objects.
- Integration-style tests covering a full turn: user → tool calls → final text response.
- Parameterize micro-variants instead of duplicating tests (schema coercion, tool discovery).
- Lightweight stubs for CLI I/O (dummy input/output) to avoid terminal dependencies.

## Coverage scope & conventions

- CI's coverage gate (`.github/workflows/coverage.yml`) measures **only** `--cov=mewbo_core --cov=mewbo_tools --cov=mewbo_ha_conversation`. The apps (`mewbo_api`/`mewbo_cli`/`mewbo_mcp`) and `mewbo_graph` are executed but **not** in the gated number — add them to see the whole picture locally. The committed root `coverage.json` is a stale point-in-time snapshot, not live output; regenerate, don't trust it.
- **Measure through `pytest-cov`'s `--cov` flags, never `coverage run -m pytest`.** The bare form instruments only the outer process, so a distributed run's workers — spawned through execnet — go uncounted and the number silently collapses toward zero for everything they executed. It is wrong in a way that reads as a coverage regression rather than as a broken recipe.
- Full local run: `.venv/bin/python -m pytest --cov=mewbo_core --cov=mewbo_tools --cov=mewbo_graph --cov=mewbo_api --cov=mewbo_cli --cov=mewbo_mcp --cov=mewbo_ha_conversation --cov-report=` then `coverage json -o coverage.json` and slice per-package from `files[].summary.missing_lines`. `testpaths` = `tests/` + `apps/{mewbo_api,mewbo_mcp,mewbo_cli}/tests` + `demo/seeder/tests` + `demo/framer/tests` — six entries, and a bare `pytest` is what collects all of them.
- Under pytest, `tests/` is on `sys.path`, so coverage tests reuse sibling helpers directly (e.g. `from test_tool_use_loop import _make_agent_context, _text_response, _tool_call_response`). Naming: broad coverage/integration suites use `*_integration.py` / `*_flow.py` / `*_extra.py`; graph wiki-plugin tool tests live in `tests/wiki/test_tool_*.py`, SCG engine tests in `tests/agentic_search/scg/`.
- New tests are integration/contract-first: drive the public caller site, assert real outputs/side-effects, stub only I/O boundaries. Reject namesake tests (a test that still passes if the production body were deleted is worthless).
- **Typechecking a module under `tests/` needs `MYPYPATH=tests`, or the green is vacuous.** `[tool.mypy] files` does not cover `tests/`, so these are checked only when named explicitly — and a sibling import (`from real_mongo import ...`) does not resolve without `tests/` on mypy's path. With `ignore_missing_imports` on, that unresolved import becomes `Any`, every annotation borrowed from it becomes `Any`, and mypy reports success while checking nothing: passing a `MongoClient` where a `CommandCounter` is required raised no error at all. `MYPYPATH=tests .venv/bin/mypy tests/<file>.py` is what actually checks. Adding `tests` to the global `mypy_path` instead would let top-level names under `tests/` (`apps/`, `demo/`, `conftest`) shadow real packages during resolution — worse than the problem.

## What a green suite cannot prove

- **In-process tests structurally cannot prove OPTIONALITY.** A test shares `sys.modules` with everything the suite already imported, so a module importable only because an earlier test enabled a subsystem still looks fine, and an "uninstalled" extra is still resident from someone else's import. Any claim about the IMPORT GRAPH or about what exists ON DISK — this package pulls in no driver, importing the stores does not import `pymongo`, auth-disabled mounts no routes — must run in a fresh interpreter. `test_iam_architecture.py:_run_probe` is the pattern: a subprocess with its own cwd and env reporting JSON, never a mock of `sys.modules`.
- **A green run is evidence about the TESTS, not the PACKAGES.** The suite covers what the suite imports, unevenly, so a package can be entirely unimportable while every test passes — a module naming a moved import raises `ModuleNotFoundError` and reaches the main branch green if no test imports it. The structural import-graph test does not close this: an edge to a nonexistent module is dropped before the cycle search, and a missing edge can only make a graph MORE acyclic. **A reference to nothing and a reference in a circle are different failures; only one shows up in the shape of the graph.**
- **A test must never touch the real `~/.mewbo`.** A bare `import mewbo_api` in a fresh interpreter lays down `cache/` and `sessions/` under whatever home resolves, so a subprocess probe pins `MEWBO_HOME` for the same reason an in-process test does. Stores derive their default path from the config chain, so any store built without an explicit `tmp_path` writes into the live config directory and persists forever. The redirect belongs in an autouse fixture in `conftest.py` beside `app_config_file` — a per-test convention leaks the first time an author forgets, and it leaks silently.
- **An injected-factory suite proves nothing about the DEFAULT factory.** Injecting a fake at every call site means the default never executes: LDAP's `_default_connection_factory` passed a float timeout to `ldap3`, which sets `SO_RCVTIMEO` via `struct.pack("LL", …)` and rejects a float, killing every real connection with a `struct.error` that is not an `LDAPException` and so escapes the driver's error normalization too. **"Stub only I/O boundaries" degrades into "stub the thing under test" the moment the injected collaborator IS the boundary.** Wherever a default factory or client exists, one test must EXECUTE the real one against something controlled — a local listener, a temp file, a captured request.
- **A green suite is not evidence for a SECURITY property.** Tests written against the implementation assert what it DID, so a secret returned to any `config.read` holder, a guard defaulting fail-open, auth re-implemented against a hardcoded token, a shim accepting expired keys and lexicographically ordered ISO timestamps all passed. Assert the PROPERTY: the value is absent from the response, an unknown permission denies, the expired key is refused. **A test that would still pass with the check deleted proves nothing about the check.**
- **High statement coverage on a resource pool proves nothing about its failure-and-release path.** The happy path (accept, dispatch, settle) is easy to cover; the arm where a launcher RAISES and the scheduler must hand the slot it already holds to the next waiter (`hypervisor.py:AgentQueue._dispatch`/`_pump`/`release`) is not. Miss it and a failed dispatch permanently shrinks the pool by one for the life of the process, suite green. Cover the exception arm and assert the pool's own counters (free slots, pending count) afterward — not merely that the call returned.
- **If a test needs to patch the thing whose behavior it is asserting, it is testing the patch.** Reaching a state by hand-draining the underlying primitive, then asserting a disjunction any error string satisfies, exercises no production code and cannot fail. Drive the real call into the real state: an over-cap spawn returns `submitted` with a live `agent_id`, appears in the liveness index while waiting, and starts when a sibling's release hands it the slot.
- **Two green suites can both be wrong when they mock each other.** Where a producer and consumer share a wire contract and each suite mocks the OTHER side, neither can detect a field-name mismatch — the feature goes inert while both stay green. Assert the actually-serialized payload round-trips through the aliases the consumer really reads, never a hand-typed fixture.
- **A green suite says nothing about COST.** Every fixture here is small, so a function scaling with stored data is indistinguishable from one that does not; the answer is never wrong, only slower forever. Pin the cost characteristic separately.

## Testing cost characteristics

Root `CLAUDE.md` → "Performance is a contract at every boundary" defines the cost
classes. A boundary that declares one needs a test that pins it, or the declaration
decays into a comment.

- **Assert READ COUNTS, never wall-clock.** A timing assertion is flaky on a loaded
  machine and, worse, passes for the wrong reason on a fast one. Seed N records with M
  children each, call the thing, and assert the number of store reads (or documents
  examined) does not grow with M.
- **Scale the dimension you claim is bounded, not the one that is obviously bounded.**
  A listing test with ten sessions of three events each proves nothing about either
  axis. Vary M with N fixed to pin `O(collection)`, and vary N to pin the bound on rows.
- **A cursor test must assert the WORK, not the payload.** An incremental fetch that
  returns nothing can still read an entire record. Asserting the response is small is
  exactly the assertion that lets that ship.
- **Fail-closed filters need a test for the unparseable case**, asserting a refusal. The
  tempting assertion — "a bad filter returns no rows" — is satisfied by returning ALL
  rows in most fixtures, because a small fixture's full set looks like a plausible
  result.

### Two tiers, and what only the second one can see

| Tier | Substrate | Contents |
|---|---|---|
| 1 — the default `pytest` run | in-process, `mongomock`, no container | almost everything; stays the fast common loop |
| 2 — `@pytest.mark.realmongo` | real `mongo:7` (`tests/docker-compose.test.yml`, `make test-mongo`) | query/round-trip counts, index behaviour, anything whose cost is the point |

Tier 2 is not "more realistic everywhere" — it is the only place certain defect
classes are observable AT ALL. `mongomock` has no query planner, so an N+1 write
and a missing index are free there; and it accepts `event_listeners=` while
implementing none of PyMongo's monitoring API, so no command can be COUNTED
against it. `tests/real_mongo.py` holds `RealMongoTier` (connection facts + the
skip rule) and `CommandCounter` (the instrument); `real_mongo_client` /
`real_mongo_commands` are the fixtures.

- **Absence of the container is a SKIP, never an error.** A developer running
  plain `pytest` must not need Docker. PyMongo connects lazily, so the skip rule
  has to `ping` — a client built against a dead address looks healthy until the
  first operation, which surfaces as a stall inside whichever assertion ran first.
- **The mongo service is the DEMO stack's shape, not the devcontainer's.** The
  devcontainer's has a named volume and is therefore persistent; a tier that
  inherits state across runs is not deterministic. No volume, no auth, isolated
  bridge network, loopback publish on its own port — the deployed and demo stacks
  own theirs, and a collision breaks a running stack.
- **Assert the SCALING, never a literal command count.** A hardcoded number is a
  snapshot of today's batch size and fails the next legitimate change;
  `10× the documents must not cost 10× the commands` survives it and still fails
  the day a per-document loop returns.
- **The double also lags the driver, and a `TypeError` from inside it is the
  tell.** pymongo's `UpdateOne` forwards a `sort` argument into the bulk builder
  unconditionally; mongomock's `add_update` has no such parameter, so ANY
  `bulk_write` of updates raises against the double while working against a real
  server. The root `conftest.py` absorbs it once — signature-guarded, so it
  self-disarms the moment mongomock grows the parameter. The rule the shim
  encodes: **fix it at the double, never by making a driver method avoid
  `bulk_write` to keep a test double happy** — that trades a real round-trip
  count for a green tier-1 run.
- **A cost assertion a no-op would satisfy proves nothing** — pair it with a read
  that the rows really landed.

## Profiling a slow or stuck test

`tests/slowplug.py` is a per-test watchdog + optional RSS sampler, structurally opt-in:
a plain `pytest` run never imports the module, so it is not a convention someone can
forget — there is nothing to disable. Load it explicitly:

```
PYTHONPATH=tests SLOWPLUG_WATCHDOG=30 SLOWPLUG_RSS_LOG=/tmp/slowplug.tsv \
  pytest -p slowplug -s <path>
```

(`-p slowplug` resolves the bare module name because `tests/` is already on `sys.path`
under pytest; `PYTHONPATH=tests` makes the same true for the `-p` import step, which
runs before that path insertion. `-s` disables output capturing, since pytest only
shows captured stderr for a FAILED test — the watchdog dump is otherwise swallowed
silently on a passing one.)

- `SLOWPLUG_WATCHDOG` (default 30s) — `faulthandler.dump_traceback_later` fires a
  stack dump for every thread if a single test's whole protocol (setup+call+teardown)
  is still running past this many seconds, repeating until the test ends. This is what
  turns "the suite hangs somewhere" into a named test with a traceback, not a guess.
- `SLOWPLUG_RSS_LOG` (unset by default) — appends one TSV row per test: elapsed
  seconds, RSS before/after/delta, and the HWM delta/total from `/proc/self/status`.

**The trap it exists to remember:** `pytest_runtest_protocol` is a `firstresult` hook.
An implementation that returns `None` runs *before* the default protocol and wraps
nothing — every timing/RSS number such a version records reads zero, silently, with no
error anywhere. It has to be a `hookwrapper` that `yield`s exactly once.

**py-spy, found the hard way:** an attach (`py-spy dump -p <pid>`) is refused here —
yama's `ptrace_scope=1` only allows tracing a process's own descendants. The parent
form is the only one that works, because py-spy then launches the target itself:

```
py-spy record -o profile.svg -- python -m pytest <path>
```

Not wired into CI on purpose — a wall-clock assertion on a shared runner is flaky by
construction and teaches people to ignore red. This is for someone investigating a
stall or a memory climb, not a gate.

## Reading a failure honestly

This repo has known order-dependent failures under full-suite load, and this machine
often runs several suites at once. A red result is a hypothesis, not a verdict.

**Three data points settle it, and fewer do not:** the same test (1) in isolation on
your branch, (2) in isolation on the base branch, (3) under the full suite.
Isolated-green plus full-suite-red is order dependence, not your regression. Red on the
base branch too is pre-existing. Only isolated-red on your branch with isolated-green on
base is a regression you introduced.

A failure count that CHANGES between two runs of the same tree is itself the signal — the
suite is load-dependent right now and no single run of it is evidence either way. Re-run
in isolation before concluding anything, and never report a full-suite green from a
loaded box as verification.

**A run that did not reach its summary line contributes NOTHING.** An interrupted run
sitting at "33%, zero failures" is not a partial green: the tests that would have failed
are disproportionately the ones that had not run yet, and a progress percentage reads
like a measurement to the next person. Report it as unstarted, and quote no number from
it — a figure in the record outlives the caveat attached to it.

**A wall-clock number from a contended box is indicative only, and must be labelled so.**
This machine routinely has several suites on it; a timing taken then measures the load,
not the change. If a timing claim matters, take it on a quiet box or do not make it —
and prefer re-using a previously measured baseline over paying for a fresh one under
contention, which degrades both numbers.
