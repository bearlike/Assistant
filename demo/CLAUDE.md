# Demo-as-code — agent guide

> ↑ [root](../CLAUDE.md)

Everything under `demo/` (plus `apps/mewbo_console/tests/demo/`) exists to
regenerate the committed doc artifacts (`docs/assets/img/mewbo-{console,wiki,search}-*`)
from a seeded, containerized stack. Read `demo/README.md` for the operator view;
this file records the engineering decisions and the traps that are NOT
re-derivable from the files themselves.

**Three surfaces, three seeders, one stack (Phase 3).** `demo/seeder` seeds all
three console surfaces from ONE `make demo-seed`, each through its own real store
contracts — no live indexer, no LLM, no network:
- **Sessions/triggers** → `DemoSeeder` (`seeder.py`) via `SessionStoreBase` /
  `TriggerStoreBase` → the Tasks surface (`mewbo-console-*`).
- **Wiki** → `WikiSeeder` (`wiki.py`) via `MongoWikiStore` (`create_project` /
  `save_page` / `upsert_nodes` / `upsert_edges` / `create_job` /
  `append_job_event` / `save_qa`) → the Wiki surface (`mewbo-wiki-*`): a
  12-project gallery, a dense Grove code graph, a wall of resumable index jobs,
  one in-flight job, and stored Q&A answers.
- **Search** → `SearchSeeder` (`search.py`) via `MongoAgenticSearchStore`
  (`save_workspace` / `create_run` / `append_run_event` / `append_past_query`) +
  the SCG store + the wiki memory store → the Search surface
  (`mewbo-search-*`): 8 workspaces, 10 completed runs, and an 86-node SCG
  capability graph. Each is an atomic seeder class, collaborators DI'd, every
  bundle model `extra="forbid"`; wall-clock instants are T0-relative OFFSETS the
  seeder rebases, so a re-seed is byte-identical.

> The two force-directed GRAPH screenshots (`mewbo-wiki-03-graph`,
> `mewbo-search-03-capability-graph`) were REMOVED as low-value. The graphs are
> still SEEDED — the live "Graph" / "Capability graph" views render and the
> search-landing stat band still reads its SCG counts — they are just no longer
> captured. That also drops the one shot that was not byte-reproducible, so
> the 16 then-remaining artifacts are byte-identical across cycles. The later
> pipeline-generated `widgets` shot (07) makes 17 today — also byte-identical
> (verified warm renders + a cold first-after-seed clean cycle) once its wait
> tracks the widget CARD's bottom jointly with the bars BEFORE `settle()`
> (see its trap table for why that ordering is load-bearing).

## Position in the monorepo

`demo/` sits ABOVE the dependency DAG, at the altitude of `tests/` — it may
import from packages and apps, but **nothing may ever import from `demo/`**.
`demo/seeder` is a uv workspace member for local dev (tests, lint, types) only;
at runtime it is NOT installed anywhere — the `seed` compose service mounts it
read-only into the api image and runs it via `PYTHONPATH` (src layout). That is
deliberate: the seeder must never force an api image rebuild, and it may only
depend on what the api image already ships (mewbo_core, pydantic, pymongo).

## Load-bearing decisions (do not "simplify" these away)

- **Isolated bridge network, never host networking.** The deployed stack owns
  host ports 5125/3001/27018 on this machine. The demo stack must be bootable
  next to it, so `docker-compose.demo.yml` uses the default bridge network with
  `name: mewbo-demo` and publishes exactly one loopback debug port (3210).
- **`:demo` image tags.** Services build `mewbo-api:demo` / `mewbo-console:demo`
  with `pull_policy: missing`. Never tag over `ghcr.io/...:latest` — the deployed
  stack pulls those.
- **Ephemeral mongo = determinism.** No volume, no auth (`mongodb://mongo:27017`,
  isolated network). Every `demo-up` is a clean world; `demo-seed` recreates it.
- **API healthcheck is authed `GET /api/sessions`, NOT `/api/models`.**
  `LLMConfig.list_models()` short-circuits on an empty `api_key` today, but the
  moment live-mode sets a key it does a real `urlopen` against `llm.api_base`
  (8s timeout) — an outbound call on an isolated network inside a healthcheck.
  `/api/sessions` is Mongo-backed and structurally can never call out.
- **Seeder writes THROUGH store contracts** (`MongoSessionStore`, trigger store).
  Stores do **not** accept `client=` injection — they build their own
  `MongoClient` from `uri`/`database` params falling back to
  `MEWBO_MONGODB_URI`/`MEWBO_MONGODB_DATABASE` env, which is exactly how the
  `seed` service targets the demo mongo. Tests monkeypatch `pymongo.MongoClient`
  (mongomock), the established repo pattern.
- **Raw app screenshots are the baseline.** The original hand-captured rasters had a
  post-processed window frame/shadow; regenerated artifacts are the raw app.
  Adding decoration back would be a post-step (ffmpeg/imagemagick), deliberately
  not done — fewer moving parts.

## Traps (each one cost real debugging time)

| Trap | Consequence | Rule |
|---|---|---|
| `docker/nginx-console.conf` proxies to `127.0.0.1:5125` (host-net assumption baked into the console image) | console silently cannot reach the api on a bridge network | mount `demo/nginx-console.demo.conf` over `/etc/nginx/conf.d/default.conf`; it is a byte-faithful copy modulo `proxy_pass` host |
| nginx auto-301s `/api/sessions` (bare) onto the SSE prefix block's `/api/sessions/`, which Flask 404s | session listing AND creation break through the proxy — this was a **live production bug** found by this stack | keep the exact-match `location = /api/sessions` block (in BOTH conf files) ahead of the SSE prefix block |
| `VITE_API_KEY` is baked into `runtime-config.js` when the console **container starts** | changing the key after boot does nothing | set env before `demo-up`; restart the console container to re-bake |
| Relative timestamps ("45 minutes ago") re-render every wall-clock minute | every capture differs → zero-diff gate can never pass | bundle stores offsets from `DEMO_T0`; seeder rebases; flows freeze `page.clock` at the same `DEMO_T0` |
| `page.clock.install` freezes JS timers | the landing typewriter pins to its first phrase (fine, deterministic); anything polling-driven never advances | mount-time fetches still fire (promise/MessageChannel-driven); do not build flows that need timer-driven refetches |
| Playwright version vs browser image | pixel drift between environments | the `shots` service image tag must equal the resolved `@playwright/test` version in `package-lock.json` — bump them together |
| The api materialises a "'title' completed" notification LAZILY on the first `GET /api/sessions/<id>/events` after a completion event exists, deduped forever on `(session_id, completion_ts)` — dismissed records included | a completion TOAST appears in whichever capture first touches a seeded session; worse, the toast text contains the session title, so an unscoped `getByText(title).first()` clicks the toast instead of the session row | the seeder's `NotificationSettler` (REST-only — `NotificationStore` is a JSON file inside the api container, no store seam exists across that boundary) touches every seeded session's events endpoint then dismisses everything, once per seed; `openSession` scopes row lookups to `main` |
| **Compositor/wall-time paints ignore the faked clock**: scrollbar-thumb fade after a scroll, CSS transitions (the card expand-chevron rotation), caret blink | ~10-pixel nondeterministic diffs that survive every clock trick and appear in a different artifact each run | the capture stylesheet in `fixtures.ts` kills them all (`transition:none`, `animation:none`, scrollbar paint off, caret transparent) — reducedMotion alone only gates keyframes, NOT transitions |
| `page.clock` fully paused BEFORE navigation starves the load path | session list pinned on "Loading sessions..." forever | `install({time: T0})` (auto-ticking) for the load, then jump-and-pause at the FIXED instant T0+60s inside `settle()` — all pending timers fire to one run-independent moment |
| `locator.screenshot()` re-scrolls and re-rasterizes on its own terms | element closeups land on two attractor scroll states → flaky byte-diffs while full-viewport shots are stable | explicit `scrollIntoView({block:"center"})`, then an INTEGER `clip` crop of the page raster via `page.screenshot({clip})` |
| Fresh named volumes are created root-owned; `shots` runs as uid 1000 | `npm ci` dies with EACCES on the cache | `shots-cache-init` (root one-shot) chowns the volume; `shots` depends on `service_completed_successfully` |
| The TurnScroller margin rail measures live scroll geometry | a few antialiased pixels jitter run-to-run in the far-left gutter | hidden by the capture stylesheet — a still carries no scroll position |

### Phase 3 (wiki + search) traps

| Trap | Consequence | Rule |
|---|---|---|
| `MongoWikiStore` reads `storage.mongodb.*` CONFIG, not the `MEWBO_MONGODB_*` env the session store honours; `create_wiki_store`/`create_scg_store` dispatch on `storage.driver` config | the wiki seeder wrote one db while the api read another → console showed an EMPTY wiki/search | the wiki seeder passes `uri=`/`database=` explicitly; AND `demo/configs/app.json` points `storage.mongodb` at `mongo:27017`/`mewbo_demo` and the `seed` service MOUNTS `./configs` — so every config-reading store (wiki/scg/search) and the env-reading session store all resolve to the SAME demo db |
| `scg.enabled=false` 503s `/scg` + empties the workspace graph; `MEWBO_AGENTIC_SEARCH_SEED` gates BOTH the fixture source catalog AND a lazy 6-workspace auto-seed on first `/agentic_search` touch | blank capability graph, blank source chips, or colliding fixture workspaces | `app.json` sets `scg.enabled=true`; api + seed set `MEWBO_AGENTIC_SEARCH_SEED=1` (catalog renders), and the seeder fills the store BEFORE any `/agentic_search` request (compose ordering), so the auto-seed's "if empty" guard skips |
| The landing "Incomplete indexes — N resumable" band reads `/jobs/recoverable` (status ∈ failed/cancelled/interrupted AND a non-no-op `ResumePlan`) MINUS active-slug jobs | `interrupted` jobs are ALSO "active" (excluded); a job whose slug has no graph/pages has a no-op ResumePlan (filtered) — so a wall of `interrupted` throwaway jobs renders the band EMPTY | seed the resumable jobs as `failed`/`cancelled` (not interrupted) AND give each a few `GraphNode`s on its own slug (the cheapest non-no-op ResumePlan; no `Project` doc needed); keep exactly ONE `finalizing` job for the "Indexing now" band |
| `DemoHelper.capturePage()` hardcoded `type:"png"` | PNG bytes written under a `.jpg` filename for every full-page wiki/search shot | `capturePage` honors `SHOTS[name].type` (jpeg-at-90 vs png), matching `captureElement` |
| A completed search run, a stored Q&A answer, and an in-flight index job each render from a STORED snapshot | needing a live LLM/POST would be nondeterministic | search run `/search?ws=&run=` (one `GET /runs/<id>` + SSE replay); wiki Q&A `/wiki/qa?answer=&page=&slug=` (static `isSnapshot`); wiki job progress replays its log via SSE `/v1/wiki/index/<id>/stream` — NOT `/events`, which is EMPTY for a seeded job |
| The wiki index wizard's Generation step fires `POST /v1/wiki/branches` (a real `ls-remote` to the repo host) | a LAN call inside a supposedly-offline capture + a nondeterministic branch list | the flow `page.route`-stubs `**/v1/wiki/branches` so the wizard falls back to "Default branch" |
| The search-landing `WorkspaceHealthBand` numbers load async from `/graph/summary` AFTER the labels render | waiting only on the labels captured blank number skeletons (also a blank↔number byte-diff) | wait for a rendered NUMBER (`86·95`), never just the label |
| `shots-cache-init` (a `shots`-profile one-shot) lingers between `compose run` invocations, pinning the network it was created on | a later run restarts it against a removed network → "network `<id>` not found" | `demo-shots-web` force-removes it first; `demo-down` names the `seed`+`shots` profiles so `down` actually tears them down |
| Two `<h1>Grove Overview</h1>` (the page title `#page-top` AND the body's first markdown heading), the platform-tile "Gitea" ALSO matching a type-tile description + a token-help link, `anthropics/claude-code` appearing 5× | strict-mode "resolved to N elements" | flows anchor on stable ids (`#page-top`), start-anchored names (`/^Gitea/`), or `.first()`; locate by the ACTUAL seeded strings (the coordinator trace lane is `coordinator`, the probes `scg-path-probe`) |

### Widget shot (07 — inline stlite widget) traps

`mewbo-console-07-widgets.png` (once a hand-composed two-window composite, now
pipeline-generated) captures a seeded session whose transcript carries a
`widget_ready` event: a Streamlit/stlite widget (the trending agent-harness
repositories card grid) rendered inline in the conversation. This is the ONE
shot that boots a real Pyodide+Streamlit render, so it has its own trap set.

| Trap / fact | Consequence | Rule |
|---|---|---|
| `widget_ready` INLINES the widget's files in its payload (`files:{"app.py","data.json"}`, frozen `extra="forbid"` at `submit_widget.py` + console `types.ts`) | seeding needs NO widget-file endpoint and NO `/tmp/mewbo/widgets` FS staging — `StliteWidgetPanel` boots the kernel straight from the event's inlined files | seed ONE Mongo `widget_ready` event via the `WidgetReadyEvent` seed kind (`models.py`); the shared `to_event(...)` kwarg contract carries `session_id` because this is the one kind whose wire payload embeds it. `requirements` MUST stay `[]` — an un-vendored pyodide wheel routes to PyPI and fails on the offline demo net |
| The demo console renders stlite OFFLINE | — | pyodide is vendored into `mewbo-console:demo` (`docker/Dockerfile.console` `npm run build` → `fetch-pyodide`); `nginx-console.demo.conf` serves `mjs`/`wasm` via its `^(/widget-host)?/pyodide/` `types` map. No api endpoint is involved |
| **Build the widget's `app.py` from NATIVE Streamlit components, NOT a hand-rolled `unsafe_allow_html` blob.** A whole-card `<a>` wrapper bleeds the theme `primaryColor` (brand clay) onto EVERY descendant that uses `color:inherit`, so the card renders "mostly orange"; and custom tiny font-sizes look nothing like a real Streamlit embed | a custom-HTML card grid reads as a cramped orange blob, not a Streamlit widget (the exact "why is it orange / why do the fonts look off" report) | use `st.container(border=True)` cards inside `st.columns`, with native `st.markdown` / `st.caption` / `st.progress`; drive color with native markdown (`:green[...]`, code spans) — you get authentic Source-Sans typography + spacing and an intentional palette (neutral titles, green gains, clay progress bars) for free |
| **The widget renders at NATURAL scale — the panel's old `zoom:0.85` is REMOVED** (fractional zoom → fractional glyph raster → janky intra-word spacing + border-AA jitter; see `apps/mewbo_console/CLAUDE.md` → "Natural scale"). And `WidgetCard` now sizes the card to content + chrome (`data-widget-chrome`), so nothing structurally clips | re-introducing any scale hack (or breaking the chrome-aware measurement) brings back both the janky text AND the clip-edge nondeterminism | design the widget's content for natural-scale pixels: the card cap is `min(600px, 70vh)` and the seeded 2×3 grid + header lands ~570px INCLUDING chrome — inside the cap with slack, no internal scroll |
| **`st.container(height=N)` (fixed-height cards) reserves a scrollbar gutter** that narrows card content by ~10px — at that width the longest bold title wrapped to 2 lines and its content overflowed the fixed box, clipping the progress bar | ragged titles + a clipped bar, i.e. the exact untidiness fixed-height was meant to prevent | equalize card heights the NATIVE way: natural-height `st.container(border=True)` + make every card render the SAME line counts (titles ≤1 line, descriptions exactly 2 caption lines — extend a too-short seed description rather than fixing heights) |
| **Text-fit verified in an interactive browser (DPR 1) can still wrap in the pipeline (deviceScaleFactor 2)** — subpixel font metrics at DPR 2 make bold text a couple px wider, and `#5 · anthropics/claude-code` sat exactly on the wrap boundary | a title that fits in your checking browser wraps in the committed artifact (caught only at the final artifact inspection) | judge text-fit ONLY on the pipeline's own PNG, never an interactive check; keep decisive slack — the widget's `title()` helper drops the owner from any slug >20 chars (`claude-code`), rank + name still identify it |
| **A console widget card is CAPPED at `min(600px, 70vh)` and scrolls overflow INTERNALLY**; content at the clip edge paints on WALL time, RACILY | if any content sits at the clip edge, that row paints (or not) run-to-run → a bottom-band byte-diff that survives the frozen clock AND the capture stylesheet | **SIZE THE WIDGET TO FIT THE CAP** so nothing is at the clip edge: 6 natural-height cards (a 2×3 grid) + header ≈570px including chrome → no scroll, no clip. More rows overflow the cap and reintroduce the race — the deliberate reason `data.json` is trimmed to 6 |
| A native card count (`[data-testid="stProgress"]`, one bar per card) reaching its target only means the DOM nodes EXIST; stlite STREAMS its render and `WidgetCard` re-measures height asynchronously (ResizeObserver → rAF), so the grid AND the card bottom can still move after the last node appears | a capture right after `toHaveCount` freezes a half-blank grid or a still-settling card edge, differing run-to-run | `page.waitForFunction` until the last bar's bottom AND the card's bottom (`.stApp`→`closest('.fixed')`) hold steady jointly across ≥8 consecutive 250ms polls, THEN `settle()`. The count alone is not a paint signal |
| ⚠️ **The geometry wait MUST run BEFORE `settle()` — the paused Playwright clock ALSO freezes `requestAnimationFrame`**, and WidgetCard's portal-mirror (ResizeObserver → rAF → state) dies with it: any card growth landing after the pause never reaches the portalled wrapper | a TRUNCATED widget card frozen mid-growth in the shot (second card row cut off; placeholder showing through beneath) — a settle-first "improvement" shipped exactly this | geometry quiesces while the clock is ALIVE; settle only freezes what the wait already verified |
| **The historical "border-AA jitter" on this shot was NEVER compositor noise — it was the rAF-freeze in miniature.** Every band-diff (~13 CSS px at the card's bottom border) was the portalled widget wrapper frozen with a rect ONE PIXEL staler than the in-flow placeholder: two `rounded-lg` borders 1px apart. Same mechanism as the truncated-card failure, at 1px magnitude | chasing it as "paint nondeterminism" (warm-up reloads, sub-pixel waits on the BARS alone) reduces the frequency without touching the cause | closed by construction: the joint wait requires the CARD (portal) bottom itself stable ≥8 polls before the clock pauses. Shot 07 is byte-identical across cycles — including the cold first-after-seed render, historically the worst case |
| `openSession` leaves the pointer wherever it clicked the landing row, often over a card | that card renders its `:hover` state in the still AND the highlight moves if the click position ever shifts (nondeterministic) | `page.mouse.move(0, 0)` before `settle()` |
| `getByText("Trending LLM Agent Harness Repositories")` matches the session-title `h2`, the sidebar row, AND the widget heading (case-insensitive substring) | strict-mode "resolved to 3 elements" | anchor ONLY on the `stProgress` count (a Streamlit testid, unique to the widget — the console renders no Streamlit); never also assert the widget title text |
| **Seeding this session PERTURBS other shots.** It is the NEWEST session (offset −600, origin `user`, default-visible), so it renders as the top row of the landing recent-list (`front`, shot 01) AND the tasks sidebar (`tasks`, shot 02) | a `make demo` regenerates 01 and 02 with different bytes — they are NOT byte-unchanged, so committing only 07 breaks the byte-reproducible invariant | regenerate + commit `mewbo-console-01-front.png` + `-02-tasks.png` alongside `-07-widgets.png` — 3 shots move, not 1. The closeups (crop to one card via `captureElement`) and the wiki/search shots (non-session home routes, no sidebar) are unaffected |
| The shot is 4:3 (1280×960); the other console shots follow their reference raster's ratio | — | a per-spec `test.use({viewport})` gives 4:3; the shared `playwright.demo.config.ts` default is used by no spec, so the VIEWPORT axis alone changes nothing else (the session-seeding axis above is what moves 01/02) |

## Keep-in-sync obligations

1. `docker/nginx-console.conf` ↔ `demo/nginx-console.demo.conf`: any change to
   the source conf beyond the `proxy_pass` host must be mirrored by hand.
2. `DEMO_T0` is a tri-contract: `demo/demo.env` (stack), the seeder's rebase
   default, and the flows' fixture default. One value, three readers.
3. Seeded content the flows locate by accessible text must match the bundles
   exactly: `shots.ts` SEED + `console-poc.json` (session titles, `npm test`,
   `src/middleware/auth.ts`); AND the wiki/search flows' inline strings +
   `wiki-helpers.ts`/`search-helpers.ts` constants ↔ `wiki-poc.json` /
   `search-poc.json` (project slugs, page ids, the `demo-project-64` job, the QA
   `answer` id, workspace ids, run ids, result-card + trace-lane text). The
   `widgets` shot's seed↔flow contract is narrower but just as brittle: the
   session title `"Trending LLM agent harness repositories"`
   (`SEED.session.widgetTitle` ↔ `console-poc.json`) AND the seeded widget's
   `app.py`, which must keep rendering one native `st.progress` bar per card so
   `widgets.spec.ts` can wait on exactly 6 `[data-testid="stProgress"]` nodes
   (its content-anchor + staleness signal). Change the seeded repo count and you
   must change the `toHaveCount(6)` in the spec too. The Phase-4 shots add their
   own seed↔flow anchors: `SEED.apiKeyLabels` ↔ `console-poc.json` `api_keys[].label`
   (settings-security), `SEED.llmApiBase` ↔ `demo/configs/app.json` `llm.api_base`
   (settings-models), and `SEED.session.planTitle` ↔ the
   `demo-scoped-api-keys-plan` session title + its `PlanProposedEvent`/
   `PlanDecisionEvent` revisions (plan-approval; a decision must fold onto an
   earlier proposal of the same revision, enforced at bundle load).
4. Playwright image pin ↔ `apps/mewbo_console/package-lock.json`.
5. The demo Mongo db name has THREE readers that must agree: `demo/demo.env`
   (`DEMO_MONGODB_DATABASE`), the `MEWBO_MONGODB_DATABASE` env on the api+seed
   services, AND `demo/configs/app.json` (`storage.mongodb.database`, read by the
   config-driven wiki/scg/search stores). Change one, change all.

## Scope boundaries

Local web screenshots only, invoked via `make demo-*` — now the full
console + wiki + search surface (20 committed `docs/assets/img` artifacts,
including the inline stlite `widgets` shot and the Phase-4 Settings +
plan-approval shots below).
Android capture (redroid + Maestro), video rendering (Playwright `recordVideo` +
ffmpeg), and CI wiring are later phases of the same effort.

**Phase 4 — legacy hand-captures brought into the pipeline.** Three shots that
used to be captured against a LIVE internal instance (leaking a real LLM proxy
host, real issued-key ids/labels + CI topology, and an internal project
codename) now render from clean seed data: `settingsModels`
(`mewbo-settings-01-models.jpg`, Settings › Models — reads
`demo/configs/app.json`'s fictional `llm.example.com` base + is-set secret
state), `settingsSecurity` (`mewbo-settings-02-security.jpg`, the issued-keys
list — seeded via the new `SeedApiKey` bundle kind, written with FIXED ids +
T0-relative `created_at` because `KeyStore.create_key` would mint a uuid + wall
clock and break byte-stability; the stored hash is salted so a seeded key can
never authenticate), and `planApproval` (`mewbo-console-03-plan-approval.jpg`,
backed by the two-revision `demo-scoped-api-keys-plan` session using the new
`PlanProposedEvent`/`PlanDecisionEvent` kinds — rev 1 decided, rev 2 left
pending is the awaiting-approval card). ⚠️ That session is `awaiting_approval`
(default-visible), so it is the top-ish row of `front`/`tasks` AND drove a
console fix: `StatusBadge` had no `awaiting_approval` arm and fell through to
the "Archived" catch-all, mislabelling the live session in the list AND its own
header. The remaining four legacy leaks are NON-console surfaces (Nextcloud
Talk, Gmail, Home Assistant Assist ×2) with no service in this stack — they
need staged captures with invented identity data, out of this pipeline's reach.
