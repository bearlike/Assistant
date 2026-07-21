> ↑ [apps/mewbo_console/CLAUDE.md](../../../CLAUDE.md) · [root](../../../../../CLAUDE.md)

# Mewbo Apps — Console Subsystem Guidance

Scope: `apps/mewbo_console/src/components/apps/` plus the colocated
`src/api/apps.ts`, `src/hooks/useApps.ts`, and the wire mirrors in `src/types/apps.ts`.
The console surface of the Mewbo Apps sub-product — a peer of Tasks/Wiki/Search,
gated by the `apps` client capability. Read the console root
`apps/mewbo_console/CLAUDE.md` first — the library-first, theming, and
shape-vocabulary rules all apply here unchanged. This file captures only the
non-obvious app-surface decisions.

## Surface shape — one ProductHero, one settings facet

- **Landing rides the shared `ProductHero`** (the peer of Tasks/Wiki/Search) — the
  `BrandMark` lives outside product dirs, don't re-import it here. `AppsLanding`
  composes the hero + intent composer + gallery grid; `AppCard` is the ONE gallery
  card and `AppStatusBadge` the ONE status pill (`rounded-full`, a state container;
  `broken` is the honest destructive tone). **`AppFreshness` always says
  something once `/system` answers** — it renders nothing only
  while the query has no data yet (loading/error). A live app with zero runs
  and zero armed triggers used to fall through to a blank render (the wiki
  `FreshnessBadge` "stay silent until there's drift" stance, copied here
  incorrectly): silence read as "fine" and hid the exact degraded state a user
  most needed to see. "Never refreshed" / "No refresh schedule" are now
  explicit `--warning`-toned states, independent so they can co-occur; the
  optional `unscheduled_pipelines` field on `AppSystemHealth` names the gap when
  the server has it.
- **The gallery cells read as ONE geometry.** `AppCard` and the loading skeleton both
  render through `cardSurface({ radius: "panel", elevation: "elev-1" })` (`ui/card-surface.ts`);
  the dashed "create" tile deliberately does NOT route through it — it hand-rolls a `border-dashed`
  `<button>` but matches the card by re-stating the SAME `rounded-xl` + `APP_CARD_MIN_H` floor, so all
  three sit on one shape while only the empty tile's fill and border style say "this one is empty".
  Icons ride the shared `Button`'s `leadingIcon` prop, never a hand-margined icon child (a `mr-1.5`
  stacks on the button's own `gap-1.5` and doubles the gap). And an apps route renders NO `<main>` of
  its own — `AppLayout` owns the single `<main>` landmark on every route, so a nested one would be an
  invalid double landmark; the landing is a plain `<div>`.
- **The management pane is a Settings facet, not a page.** `AppsPane` (zero-prop,
  `React.lazy`, chrome-agnostic — the pane contract) registers on the `apps` facet
  in `settings/panes.ts`. ⚠️ **Facet + `x-group` lockstep still applies**: the `apps`
  `FacetId` in `facets.ts` and the pane registration move together, or the pane is a
  compile error / the section silently buckets into "Other". This is a pane-only
  facet (no backend schema section), so there is no `x-group` half to regenerate —
  but the `FacetId`/`FACETS`/`FACET_ICONS` trio is still one edit.

## Rendering — an app renders in an IFRAME; a chat widget renders in-document

**`AppFrame` is the app render surface, and the iframe is load-bearing, not
styling.** It replaced an in-document `@stlite/react` mount (`StliteAppPanel`,
deleted). The concrete failure: stlite runs Streamlit in the HOST document, and
Streamlit's multipage navigation calls `history.pushState(basePath + "/" +
pageName)` on EVERY page switch (its `handleNavigation` → `maybeUpdatePageUrl`).
wouter monkey-patches `history.pushState` globally to observe programmatic
navigation, so it saw those pushes, re-ran the console's `<Switch>` against a
path `parseAppsRoute` could not parse, and unmounted the app the user was
looking at — a sidebar page click bounced you to the Apps gallery.

- **There is no app-side fix, so don't look for one.** stlite's only pushState
  guard is `protocol === "file:"`; `embed` mode doesn't suppress it; setting
  `basePath` only changes the prefix. `st.navigation`/`st.Page` does NOT avoid
  it either — both MPA flavors emit the same `Navigation` message through the
  same URL-writing path, and `position="hidden"` only hides the nav links. The
  one app-side avoidance is a genuinely single-page app switching views via
  `st.tabs`/`session_state`, which never fires a page change.
- **A frame owns its own `window.history`, `document.title` and style scope**, so
  the fix generalizes past that one symptom: the title guard and the
  style-scoping guard that the in-document widget panel needs have nothing to
  defend against inside a frame. Treat host contamination as ONE class, not
  three patches.
- **Same-origin iframe = a CONTAMINATION boundary, NOT a security boundary.**
  `allow-same-origin` is mandatory (the injected SDK fetches `api_base` on the
  console's origin with no CORS headers; Pyodide fetches its runtime/wheels; its
  worker + WASM bootstrap are unavailable to opaque origins), and granting it
  beside `allow-scripts` means frame code can reach `parent.document`. What the
  sandbox DOES buy is the omission of `allow-top-navigation*`, which is exactly
  the host-hijacking class that motivated the frame. Don't market it as
  isolation from hostile app code — the render-token model owns that.
- **It converges surfaces (the DRY win).** Aura already rendered apps by posting
  `mewbo-app-payload` into `widget-host.html`; the console was the last consumer
  mounting stlite in-document. There is now ONE render path for every embedder.
- **The chat widget card still mounts IN-DOCUMENT** (`StliteWidgetPanel`, portalled
  to `<body>`) and therefore still needs `useTitleGuard` + `useStliteStyleScope`.
  Do not delete those as "iframe made them redundant" — they defend the surface
  that is still in the host document.

### Two traps the frame introduces

- **The host page is at a DIFFERENT PATH in dev and prod, and both return 200.**
  Prod serves the separate relative-base build at
  `/widget-host/widget-host.html`; the dev server has no such build and serves
  the root HTML file at `/widget-host.html`. Request the prod path in dev and
  Vite's SPA fallback returns `index.html` — the console renders RECURSIVELY
  inside the app frame with no error anywhere. `AppFrame` branches on
  `import.meta.env.DEV`.
- **`/widget-host/` MUST be on the SW `navigateFallbackDenylist`**
  (`vite.config.ts`). An iframe load IS a navigation request, so without it
  workbox's `NavigationRoute` claims it and serves the precached `index.html` —
  the same recursive render, but only once a service worker is active, so it
  survives dev and a hard-reloaded first visit and appears on the SECOND load.
- **nginx needs no change for correctness, but does for caching.**
  `/widget-host/widget-host.html` is a real file, so the SPA `location /`
  `try_files` already serves it. Its assets under `/widget-host/assets/` do NOT
  match the `/assets/` prefix block though, so they inherited `no-cache` — and
  the console now loads that multi-megabyte stlite bundle on every app open. An
  `^~ /widget-host/assets/` block gives them the same immutable caching
  `/assets/` gets (content-hashed JS/CSS; version-stamped `.whl`s).

## The boot seam — generalize, don't fork

`src/widget/stliteBoot.ts` is SHARED with the chat-card widget renderer. It was
generalized, not copied: one private `kernelOptionsFor` that both
`buildKernelOptions` (widget) and `buildAppKernelOptions` (app) funnel through —
`stliteBoot.test.ts` locks the output shape. Since the streamlit-facade change that shared funnel
ALSO injects the platform theme (pinned `streamlit-facade` requirement, the
wrapper entrypoint, a seeded `.streamlit/config.toml`) into
BOTH paths — see the console root CLAUDE.md "Platform theming" section for the
traps before touching any of it.

- **The injected wrapper wears the AUTHORED entrypoint's path; the authored file
  is relocated to a private sibling.** Streamlit derives the multipage sidebar's
  MAIN-PAGE label from the entrypoint script's FILENAME, so the old fixed
  `_mewbo_main.py` wrapper rendered to users as "Mewbo Main" — a platform
  implementation detail leaking into the app's own navigation. `relocatedEntrypoint`
  prefixes `_mewbo_app_` onto the original basename and keeps its DIRECTORY,
  because sibling-module imports resolve relative to it (a root-level private
  name would break `pages/home.py`'s neighbours). The authored bytes are copied
  unmodified, so traceback line NUMBERS stay correct and only the displayed
  filename changes.
- **`basePath` is pinned explicitly.** Unset, stlite defaults it to
  `window.location.pathname` at kernel init, which is only right when the page
  IS the app. Inside the frame the pathname is the widget-host URL, so any
  surface whose hosting page isn't the app's own must pass it.

- **`buildAppKernelOptions` takes the multi-file `AppFrontend` map + entrypoint and
  INJECTS `_app_context.json`** (`{token, api_base, app_id}`) into the files. The
  injected context WINS over any same-named file in the app bundle — the app can't
  smuggle its own context. Same-origin serving: `api_base = API_BASE ||
  window.location.origin`, so the render token + SDK talk to the same host with no
  CORS.
- **The SDK (`mewbo_app.py`) is injected server-side, not here.** The backend folds
  it into `frontend.files` at the detail seam (see the API-side CLAUDE.md), so the
  console bundles NEITHER the SDK nor `_app_context.json`-vs-SDK ordering logic — it
  only adds the runtime context. Don't re-add a client-side SDK copy.
- **Shared panel chrome lives in `src/components/stlitePanel.ts`** (`.ts`, NO JSX —
  theme/title guard hooks + `STLITE_STREAMLIT_OVERRIDES` + `PYODIDE_URL`). It is now
  consumed by `StliteWidgetPanel` (in-document, needs every guard) and, from
  `AppFrame`, only `useConsoleTheme` — the frame gets the Streamlit DOM overrides
  from the host page itself. Keep the module: the widget panel still depends on all
  of it.
- **The handshake is ready-signal driven and the `src` assignment is ordered.**
  The host posts `mewbo-widget-host-ready` exactly ONCE and silently drops a
  payload posted before it, so a missed signal hangs the app forever with no
  error. `AppFrame` therefore assigns `frame.src` inside the effect AFTER the
  message listener is attached — `src` in JSX starts loading during React's
  commit, i.e. before effects run, so a warm-cached host can fire ready into a
  window that isn't listening yet. Don't move `src` back into JSX and don't
  paper over it with a retry timer.
- **Re-posting the payload IS the theme update path.** The host tears down and
  remounts the kernel on a re-post, which is the only way to re-theme an
  init-only kernel. No second message type. The payload itself is held in a ref
  and read at post time, so a parent re-render (the caller builds `appContext`
  inline) can't reboot Pyodide by identity change alone.
- **Render token keyed by version + scope.** `useAppRenderToken(appId, version,
  enabled, writable)` mints via `POST /token`; `AppFrame` is keyed on
  `token_id` so a rollback / re-mint remounts the kernel (kernel options are
  init-only — a live kernel can't be re-themed or re-tokened, only remounted).
- **Write-back token.** `AppDetail` computes `writable =
  spec.pipelines.some(p => p.user_writable)` and passes it so the hook requests
  `scope:"write"` IFF the app declares a `user_writable` (form-input) pipeline —
  the ONLY case the served frontend's `app.pipelines.submit` needs. The server
  403s a `write` mint for any app that doesn't (least privilege is structural, not
  client-trusted), and the console holds the master key server-proxied, which the
  `write` mint requires. `AppDetail` injects the minted token's `scope` into the
  `AppContext` so the injected SDK knows whether `submit` is allowed; a read-token
  page fails the write client-side, never a silent 403.

## File map — `AppDetail.tsx` split on the `SectionId` seam

`AppDetail.tsx` is the root component only (status-branch renders + the
rendered-app pane); everything else lives under `railSections/`:
`AppDetailHeader.tsx` (header + pause/resume), `InstrumentRail.tsx` (the
collapsible-cluster chrome — `InstrumentSection`/`CountPip`/`WarnPip`/the
collapsed icon strip), and one file per section body — `Health.tsx`, `Pipelines.tsx`,
`Schedule.tsx` (trigger rows), `Runs.tsx`, `Versions.tsx`. `flash.ts` holds the
"flash, don't toast" timing (`FLASH_SUCCESS_MS`/`FLASH_ERROR_MS`, used by
`Health`'s rearm confirmation and `Pipelines`' fire-now result). `rows.tsx` is
the instrument rail's ROW vocabulary (`RailRow`/`RailList`/`RailMeta`/
`RailNote`/`RailStat` + `RAIL_ROW_CLS`/`RAIL_TONE_CLS`, with `RailEmpty`
re-exported from the NavRail kit) — every section body renders rows through
it; the boundary with `nav-rail/rows.tsx` is documented in that kit's module
docstring. Add a new section by adding a file here
and wiring it into `InstrumentRail.tsx`'s `SECTION_META` + JSX — never grow
the root file again.

## Detail rail — a collapsible status-first instrument cluster

`AppDetail`'s right rail is an `InstrumentRail`: sections on the vendored shadcn
`Collapsible`, ordered by operational urgency — Health and Recent runs default
OPEN (live state), Pipelines / Schedule / Versions default collapsed
(configuration/history). On lg+ the whole rail collapses to a 52px icon strip
(one glyph per section, each re-expands); persisted under `mewbo:apps-rail-open`
(mirrors `mewbo:sidebar-open`). Per-section open state is deliberately ephemeral
— do not add per-section persistence. Vocabulary distinction that took a review
to settle: a **warning is a state, not a count** — Health signals degradation
with a `rounded-full` `--warning` dot (state container), while runs/versions get
the 4px count pip; the warn dot also rides the collapsed strip's Health glyph so
a degraded app stays visible with the rail closed (the `AppFreshness`
always-says-something law, extended to the collapsed state). The Pipelines
section renders only when the server reports liveness rows — on an older server
the field's absence ≠ "no pipelines", so hiding beats a false empty state.

Every rule the rail draws — each `InstrumentSection`'s top border, the cluster's
closing bottom rule, and the aside's lg left edge — rides the kit's
`--rail-border` token, the same one the NavRail's zone dividers use, so a
boundary reads identically on both sides of the app. `--rail-border` already
embeds its alpha (`H S% L% / A`); never append another `/NN` or the browser
drops the whole declaration (a `--border`-family value at its inside-a-card
alpha reads as no line at all against a panel edge, which is the bug this
replaced). This is the border half of the shared-vocabulary boundary: the
instrument rail takes the kit's tokens, glyph sizes, `RAIL_PAD_X`, `RAIL_GUTTER`,
`RAIL_INDENT` and `RailEmpty`, keeping only its own collapsible section chrome
(`InstrumentSection`, an axis the kit's static `RailSection` deliberately does not
model) and its own two-line instrument row — see `nav-rail/rows.tsx`'s module
docstring. It composes the SAME rules-vs-fills geometry as a labelled NavRail
section: `InstrumentSection`'s body nests `RAIL_GUTTER` (floats the row fills off
the aside's left border) around `RAIL_INDENT` (steps the body in under its
heading), on separate wrappers so the 4px + 6px stack instead of colliding on
`padding-left` — so the row's FILL floats 10px in (gutter 4 + indent 6) and its
ICON lands at 18px (+ the row's own `RAIL_PAD_X` 8), matching the nav side's
labelled leaf row row-for-row. The two rails read as one system.

## Build progress — reuse `useSessionEvents`, resolve on the real event

The creation flow tails the builder session's EXISTING `useSessionEvents` SSE
stream (`?session=` in the route carries the builder session id) — NO bespoke
transport, NO synthetic timer. `BuildProgress` shows real activity and resolves on
the real `app_ready` terminal event OR the detail's transient-status poll
(`draft`/`building` → 2s poll → flips to live). The `app_ready` payload is `{app_id,
title, summary, version}` (the build-phase terminal wire event).

## host.ts — the `mewbo-app-payload` back-compat contract

`src/widget/host.ts` (the standalone `widget-host.html` embedder, chiefly the Aura
WebView) dispatches on the wire `type`, keeping BOTH shapes:

- `mewbo-widget-payload` (existing widget) — UNCHANGED, tested return shape stable.
- `mewbo-app-payload` (new) — `{entrypoint, files, requirements, app_context:{token,
  api_base, app_id}}`. `parseAppMessage`/`parseHostMessage` validate defensively (a
  WebView shares one `message` bus) and ignore anything else silently.

Dispatch is on the wire `type`, NOT an invented `kind` — that's what kept
`parseWidgetMessage`'s tested return shape intact. Aura mirrors this exact shape;
renaming a field here is a cross-surface break.

## API client — `readJson` reads `data.message`

`src/api/apps.ts` re-derives its own `withBase`/`jsonHeaders`/`readJson` (mirrors
the `agenticSearch.ts` precedent — a small deliberate divergence from `httpBase`,
not a third fetch quartet to grow). Every request advertises
`X-Mewbo-Capabilities: apps` so the create endpoint scopes the builder session.
**Its `readJson` reads the error body's top-level `message`** — which matches the
backend's `shape="message"` envelope exactly (the API emits `{"message": ...}`), so
no envelope adapter is needed; a 4xx surfaces the server's message verbatim.
`mintAppToken` + `TriggerDTO` are kept (used by `useAppRenderToken` /
`AppSystemHealth`); the granular trigger client calls were pruned (the health strip
reads triggers from `/system`).

## Run-now + Re-arm — the on-demand refresh controls

`PipelineRow` gains a per-pipeline "Run now" (POST `/pipelines/<name>/fire`) and
`HealthBody` a "Re-arm schedules" action (POST `/rearm {seed:true}`, shown when
`unscheduled_pipelines` is non-empty). Decisions that took a review to settle:

- **Flash-don't-toast, and the flash must not be gated by the state it clears.**
  `RearmAction`'s success flash lived inside the `unscheduled.length > 0` block —
  a SUCCESSFUL seed empties `unscheduled_pipelines`, the `onSettled` invalidation
  refetches `/system` inside the 3s window, the gate flips, and React unmounts the
  flash exactly when the action worked (errors kept theirs — inverted feedback).
  The flash state + timer now live in `HealthBody` (the mutation too;
  `RearmAction` is presentational) and the container renders on
  `(unscheduled || flash)`. Generalize: any transient confirmation whose success
  MUTATES its own mount condition must hoist its state above that condition.
- **Per-row mutation instances** (`useFirePipeline()` mounted inside each
  `PipelineRow`, args at `mutate()` time — the `useRollbackApp` shape) give
  independent `isPending` per row; rows are keyed by pipeline `name`, so a
  `/system` refetch never remounts a row mid-flash.
- **Wire truths:** the code arm's `cache` is `"hit" | "miss"` (a Literal, NOT a
  boolean — both strings are truthy, the landmine class); the agentic 202 carries
  `status: "started" | "steered"` and NEVER `"refused"` (a refused wake is a
  backend 409 `{message}`); 429 additionally carries `retry_after_seconds`
  (`FirePipelineError.retryAfterSeconds`, appended to the flash as "retry in Ns").
- **202 means "data arrives later": ride the existing 30s `/system` poll** via the
  `appSystemKey` invalidation — no new poller, no run-completion subscription.
  The flash text distinguishes "Refreshed" (200 code) from "Refresh started"
  (202 agentic) because a 202 hasn't produced anything yet.

## Wire mirror discipline

`src/types/apps.ts` mirrors the Python contracts snake_case, frozen (three-way mirror:
Python ↔ this ↔ Aura Kotlin). `AppDataDoc` is the ENVELOPE (`{app_id, collection,
key, doc, updated_at}`) — the SDK unwraps `row["doc"]`, the console's type is the
whole envelope. Extend from the Python `models.py`, never redefine a shape here.
`AppVersion`'s additive `summary?: AppVersionSummary` / `verification?:
Record<string, AppVersionVerificationOutcome>` are the same discipline applied
to version transparency — both mirror `models.py`'s `AppVersionSummary`/
`PipelineVerdict` field-for-field and stay optional so a pre-existing version
row (recorded before either field shipped) simply lacks them; every reader must
tolerate absence rather than assume presence.

## Version transparency — diff summary + per-pipeline verification badges

The Versions rail renders what a submit/rollback actually changed, sourced
entirely from the additive wire fields above rather than re-deriving a diff
client-side.

- **`versionSummary.ts`'s `describeVersionSummary` is the pure, React-free
  formatter** (same convention as `pipelineSchedule.ts`/`triggers/triggerFormat.ts`)
  — it composes a compact line ("2 files changed · +1 pipeline (meetings)") and
  returns `null` when every count is zero and every list empty, so the caller
  (`VersionsBody` in `railSections/Versions.tsx`) renders NOTHING rather than a
  fabricated "no changes" placeholder. Note the FE's line format is its own
  composition, not a wire value — the backend's `AppVersionSummary.describe()`
  string is never sent over the wire, only the structured counts/lists it's
  computed from.
- **`VersionsBody` is exported from `railSections/Versions.tsx` for direct
  unit-render** (colocated `../versions.test.tsx`, following the
  `agentic_search/RightRail.test.tsx` idiom) rather than only reachable through
  the full detail mount. It renders
  the summary line under each version's note, plus one verification badge per
  pipeline (reusing the shared `Badge` — emerald/red/muted + ✓/✕/– glyphs, never
  color alone) when that version's `verification` map is present. Defensive
  reads throughout: an old row with neither field renders exactly as it did
  before either landed.

## Detail-screen freshness — `useApp` settled-state polling

`useApp` (`hooks/useApps.ts`) polls every 30s once the app is settled (past the
transient 2s draft/building poll) and also refetches on window focus. Neither a
repair, a rollback, nor an agent editing its own app via `get_app`/`submit_app`
pushes a signal to an already-open detail screen (`app_updated` is emitted
server-side but not consumed anywhere — see the API-side CLAUDE.md's accepted
v1 gap), so without this a user watching the app would see no change until a
manual reload. This is the console-side half of closing that gap; the
`app_updated` event itself remains unconsumed.

## Accepted v1 gap

`app_updated` (emitted by the backend on repair/rollback) is NOT consumed — a
live-open detail screen refreshes manually, not on the event. Auto-refresh on repair
is phase 2 (documented in the API-side CLAUDE.md too).
