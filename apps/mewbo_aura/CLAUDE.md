> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [data](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) · [ui](app/src/main/java/com/mewbo/aura/ui/CLAUDE.md) · [voice](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md) · [notify](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md) · [di](app/src/main/java/com/mewbo/aura/di/CLAUDE.md) · [mock](app/src/main/java/com/mewbo/aura/mock/CLAUDE.md) · [debug](app/src/debug/java/com/mewbo/aura/CLAUDE.md) · [test](app/src/test/java/com/mewbo/aura/CLAUDE.md) · [redroid](tools/redroid/CLAUDE.md)

# Mewbo Aura — Android Assistant Client

Scope: `apps/mewbo_aura/` — native Kotlin + Jetpack Compose assistant app
(orb overlay via the system assistant role + sessions/chat UI) over the
existing Mewbo session REST+SSE API. The backend owns all logic; Aura is a
thin, polished frontend. **Spec lineage: v1 → design v2 →
v3 (model picker, composer options + attachments, tool-call fold) →
v4 (default project + scope indicator + multi-line composer;
text-first overlay handoff) → v5 (voice-first — auto-listen on
trigger, first turn streams in-overlay as a response card, second turn
onward hands off; aurora shader rebuild) → the Settings wave (per-tool
device-capability toggles gated at the ONE `DeviceToolGate` seam — catalog
intersect + executor `tool_disabled` refusal; a Streamlit-widgets flag; per-surface
model defaults — app vs assist overlay, independently persisted) → the widget wave
(Streamlit widget renderer: `widget_ready` event → reducer fold → a
`WebViewAssetLoader` card serving the console-built offline stlite bundle bundled
into the APK; `stlite` capability advertised + rendered at the SAME
`streamlitWidgetsEnabled` seam; overlay shows a summary card, not the WebView)** —
deviations are recorded there, never in ad-hoc docs here.

## CLAUDE.md tree (this app)

The tree grew to ~29 files — read the deepest one that applies before editing. Nearly every
substantive package now has its own CLAUDE.md so future session learnings accrue at the level that
owns them; a new substantive package gets one.

**Design system: [`DESIGN.md`](DESIGN.md) is the canonical statement of the visual laws, token
values, provenance, and the regression registry ("never again" list).** Read it before ANY
visual/UI change; when a scoped CLAUDE.md and DESIGN.md disagree, DESIGN.md wins and the
CLAUDE.md is stale. Headline laws that have already been shipped wrong once: resting state is a
solid background with NO aura (aura = blue bottom glow, invocation/run-gated only); the chat top
bar stays transparent; tool activity renders above the response (reducer-enforced); the
disclaimer anchors once at the transcript end.

| Scope | File |
|---|---|
| **Design system: laws, tokens, provenance, regression registry** | [`DESIGN.md`](DESIGN.md) |
| App root: build, device matrix, layering, atomic-class rules, iconography | `CLAUDE.md` (this file) |
| **Data — hub**: layering + the advertise/answer seam | `app/src/main/java/com/mewbo/aura/data/CLAUDE.md` |
| Data · REST (`AuraApi`, DTOs, `Response<>`-wrapped-vs-plain throw) | `app/src/main/java/com/mewbo/aura/data/api/CLAUDE.md` |
| Data · SSE (`SessionStreamClient`, backlog replay, `trySend` hazard) | `app/src/main/java/com/mewbo/aura/data/sse/CLAUDE.md` |
| Data · model (`SessionEvent`, `TranscriptReducer`, POISON ANCHOR, `PromotedTools`) | `app/src/main/java/com/mewbo/aura/data/model/CLAUDE.md` |
| Data · repos (`RunRepository` `@Singleton`, fork/retry, `RunNotifications`) | `app/src/main/java/com/mewbo/aura/data/repo/CLAUDE.md` |
| Data · device tools (catalog/executor, two-layer gate, launch gate) | `app/src/main/java/com/mewbo/aura/data/device/CLAUDE.md` |
| Data · settings (`SettingsStore` keys, `KeystoreCipher`) | `app/src/main/java/com/mewbo/aura/data/settings/CLAUDE.md` |
| **UI — hub**: thin router, Compose stability, one-chat-tree, a11y | `app/src/main/java/com/mewbo/aura/ui/CLAUDE.md` |
| UI · theme (tokens, discipline, `AssistantExtras`, reduced-motion) | `app/src/main/java/com/mewbo/aura/ui/theme/CLAUDE.md` |
| UI · orb + shared shader primitives (`GlslNoise`/`ClayFlowerSdf`) | `app/src/main/java/com/mewbo/aura/ui/orb/CLAUDE.md` |
| UI · shader family (`AuroraEdgeGlow`/`AuroraWashTop`) | `app/src/main/java/com/mewbo/aura/ui/aurora/CLAUDE.md` |
| UI · chat surface (`ChatScreen`/`Transcript`, message actions, scope row) | `app/src/main/java/com/mewbo/aura/ui/chat/CLAUDE.md` |
| UI · promoted-tool action cards (`PromotedTools` allowlist trap) | `app/src/main/java/com/mewbo/aura/ui/chat/toolcards/CLAUDE.md` |
| UI · Streamlit widget card (WebView ready-signal) | `app/src/main/java/com/mewbo/aura/ui/chat/widget/CLAUDE.md` |
| UI · composer (`AuraComposer`, `RmsWaveform`, scope-row anchor) | `app/src/main/java/com/mewbo/aura/ui/composer/CLAUDE.md` |
| UI · assist overlay render (`AssistUiState`) | `app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md` |
| UI · shared vocabulary (`ActionSheet`, `MarkdownMessage`, `ErrorCard`…) | `app/src/main/java/com/mewbo/aura/ui/common/CLAUDE.md` |
| UI · drawer + routes + `SessionActionsSheet` | `app/src/main/java/com/mewbo/aura/ui/navigation/CLAUDE.md` |
| UI · recents view-state + rail helpers | `app/src/main/java/com/mewbo/aura/ui/sessions/CLAUDE.md` |
| UI · chat search (client-side, title-only) | `app/src/main/java/com/mewbo/aura/ui/search/CLAUDE.md` |
| UI · settings screen (7 sections) | `app/src/main/java/com/mewbo/aura/ui/settings/CLAUDE.md` |
| **Turn-completion notifications** (FGS follow-a-run) | `app/src/main/java/com/mewbo/aura/notify/CLAUDE.md` |
| **DI** — Hilt modules + the seam law | `app/src/main/java/com/mewbo/aura/di/CLAUDE.md` |
| **Mock backend** (debug scripted backend) | `app/src/main/java/com/mewbo/aura/mock/CLAUDE.md` |
| Voice: `AssistTurnMachine`, `VoiceInteractionSession`, speak-along | `app/src/main/java/com/mewbo/aura/voice/CLAUDE.md` |
| Debug-variant tooling (preview hosts, fakes, mock) | `app/src/debug/java/com/mewbo/aura/CLAUDE.md` |
| JVM test idioms: coroutine-scope house rules, scripted-fake traps | `app/src/test/java/com/mewbo/aura/CLAUDE.md` |
| redroid dev container ops | `tools/redroid/CLAUDE.md` |

## Standalone Gradle project inside a Python monorepo

This directory is its own Gradle root (`settings.gradle.kts` lives HERE, not
at the repo root). Nothing Android leaks upward: no root-level gradle files,
caches, or SDK paths. Consequences:

- Build entry point is ONLY `./gradlew` from this directory. No system Gradle.
- `ANDROID_HOME` is not exported in CI/agent shells — prefix commands:
  `ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug`.
- `local.properties` (gitignored) carries `sdk.dir` for IDE use.
- Repo-wide Python tooling (ruff/mypy/pytest) ignores this subtree; Kotlin
  quality gates run through Gradle only.

## Build → install → verify loop (mandatory after UI/data changes)

```bash
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug
adb -s localhost:5555 install -r app/build/outputs/apk/enterprise/debug/app-enterprise-debug.apk
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity
adb -s localhost:5555 exec-out screencap -p > /tmp/aura.png   # then LOOK at it
adb -s localhost:5555 logcat -d --pid=$(adb -s localhost:5555 shell pidof com.mewbo.aura) | grep -E "FATAL|AndroidRuntime"
```

**Every adb/gradle invocation passes `-s <serial>`** — never default device
selection; redroid and the Pixel can be connected simultaneously.

**Distribution flavors (`public` · `enterprise`).** `enterpriseDebug` is the dev/enterprise
variant — it bakes a private, deployment-supplied root CA as a Network Security Config trust
anchor and permits cleartext for LAN backends, so the loop above builds
`assembleEnterpriseDebug`. `public*` carries platform trust only (system CAs) and is the
ONLY flavor CI builds + ships to GitHub. The umbrella `assembleDebug`/`assembleRelease`
build BOTH flavors — always name the flavor. The CA cert is gitignored and auto-seeded
from `~/temp_folder/enterprise-ca.crt` by the `seedEnterpriseCa` task (override with
`-Pmewbo.enterpriseCaSource=` / `AURA_ENTERPRISE_CA_SRC` / `AURA_ENTERPRISE_CA_B64`); an enterprise
build fails loudly if it can't find the cert. **Never again:** `public*` APKs must not
contain the CA — verify with `unzip -l <apk> | grep enterprise_ca` (public = empty, enterprise
= `res/raw/enterprise_ca.crt`). Prereleases (`0.0.XX-debug`) ship `enterpriseDebug`, built
locally and attached to the release by hand.

**Before any `assemble*Release`: build the console first** (`npm run build` in `apps/mewbo_console`,
which also emits `dist/widget-host/`). A RELEASE build FAILS HARD when that bundle is
absent (`syncWidgetHostAssets<Variant>` — a release must never silently ship the widget feature
advertised-but-broken); debug builds only warn. Aura releases are built LOCALLY (above), so this is a
one-command prerequisite, not CI wiring.

## Device matrix

| Tier | Device | Use for | Hard limits |
|---|---|---|---|
| 1 | redroid container (`tools/redroid/`, `localhost:5555`) | UI, chat, streaming, reducer work (M1–M3) | AOSP: **no `SpeechRecognizer`, no TTS engine** → always `FakeTranscriber`/`FakeSynthesizer`; software GPU → FPS numbers meaningless; assistant role/gesture not representative |
| 2 | physical Pixel over Wi-Fi adb | assistant role, gesture overlay, real STT/TTS, haptics, all M4/M5 acceptance | needs a human in the loop |

redroid ops (binder module load, subnet pin, `data/` wipe, backend line-of-sight, GMS variant) MOVED
to [`tools/redroid/CLAUDE.md`](tools/redroid/CLAUDE.md) — read that before touching the container.

## Hard-won build facts (don't re-derive)

- AGP 9.2.1 has **built-in Kotlin** — applying `org.jetbrains.kotlin.android`
  hard-errors. Its internal Kotlin is pinned to 2.4.0 via `buildscript
  classpath` in the root build file to match the compose/serialization
  plugins; jvmTarget lives in top-level `kotlin { compilerOptions {} }`.
- compileSdk 37 is forced by AAR floors (lifecycle 2.11, hilt-nav-compose
  1.4, mikepenz markdown 0.43) — don't "fix" it back to 36.
- KSP 2.3.9: Hilt modules/`@Provides` must never be `internal`
  (google/ksp#2964 codegen break).
- dagger 2.60's POM omits `error_prone_annotations` but its ViewModel-
  component codegen references it — the catalog carries the dep explicitly;
  removing it breaks the first `@HiltViewModel`.
- material3 1.4.0's `MaterialExpressiveTheme`/`MotionScheme` are `internal`
  (unreachable, and `javap` misleadingly shows them public — Kotlin
  visibility is frontend-enforced). Plain `MaterialTheme` + `AuraMotion`
  tokens is the settled v1 approach, not a stopgap.
- Gradle 9.6 removed `project.exec` — build-script subprocess calls use
  `ProcessBuilder` (see the release-signing keytool bootstrap).
- `android.net.Uri` can't be hand-doubled in plain-JVM unit tests
  (package-private ctor, real methods throw in the stub) — the catalog
  carries `mockito-core` solely for tests needing a behaving `Uri`. See
  `app/src/test/java/com/mewbo/aura/CLAUDE.md` for how to actually use it, plus other JVM-test-only
  traps (coroutine-scope house idiom, scripted-fake replay semantics, ISO-timestamp parsing).
- `androidx.compose.material:material-icons-extended` was added 2026-07-14 (BOM-managed by the compose
  BOM → 1.7.8; per the § Iconography rule). R8 strips the unused glyphs in `release`; `debug` carries
  the full aar. Its glyphs DEPEND on `material-icons-core`, so a jar grep for a CORE glyph (`Build`,
  `Settings`) inside the extended aar returns 0 — that's expected, not a missing dependency.

## Recurring-401 rule + credential seeding

A 401 from the app on the dev device almost always means **the app's stored
API key is gone** (container recreated, `data/` wiped, or a fresh install)
— NOT a backend/auth bug. Don't re-diagnose; re-seed deterministically
(debug builds only):

```bash
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity \
  -e seedBaseUrl http://api:5125 \
  -e seedApiKey "$(grep '^MASTER_API_TOKEN' "$(git rev-parse --show-toplevel)/docker.env" | cut -d= -f2)"
```

Never hand-type the key through the Settings UI in automation — `adb input
text` mangles long secrets.

## Parallel agents in one worktree

Multiple implementer agents share this exact working tree concurrently
(design-v2 waves). Three real mechanisms destroyed or misattributed work
before these rules existed:
- `git checkout -- <foreign-path>` reverts to the last COMMIT, destroying
  any OTHER agent's uncommitted edits to that path — not just your own.
- A tree-wide `git stash` (even "isolated verify, restore right after")
  opens a window where every other agent's `ls`/`Read` sees the pre-stash
  tree — reads as a false "work lost" alarm.
- A bare `git add <your files>` + bare `git commit` commits the WHOLE
  shared index, including any OTHER agent's already-`git add`-staged,
  not-yet-committed files (one agent's deletion rode into another's commit
  this way).

Rules: never run `checkout --`/`clean`/`reset`/`stash` on a path outside
your own lane; commit with an explicit pathspec
(`git commit -m "..." -- <exact paths>`) or inspect
`git diff --cached --stat` first; commit your own coherent slice early
rather than waiting for a whole-tree green (tree-wide red from someone
else's in-flight work isn't yours to fix); verify tree state with
`git diff`/`git status`, never a bare `ls`/`grep`/`Read` — those can return
stale reads mid-churn. Gradle's `UP-TO-DATE` is untrustworthy here too —
force `--rerun` on at least one verification pass before trusting a result.

Run `:app:lintDebug` at every wave boundary, not just at the end — the
missing `VIBRATE` permission silently no-op'd every haptic (including v1's)
for a full release cycle because lint never ran until the final gate.

## Debug-variant tooling (src/debug)

Debug builds ship: `LivenessShowcaseActivity` (5-page design-v2 visual
gallery — aurora wash/glow/edge, spark, and the orb states; supersedes the
deleted `OrbShowcaseActivity`), `ChatPreviewActivity` (chat vocabulary on
fake state, no network), `AssistOverlayPreviewActivity` (fast-loop overlay
verification, no assistant-role/gesture needed — see `voice/CLAUDE.md`),
the fake voice pipeline
(auto-selected where `SpeechRecognizer` is unavailable), and the **mock
backend** (`mock/` — a debug-only `OkHttpClient` interceptor, contributed
via a Dagger `Set<Interceptor>` multibinding in `di/MockBackendModule.kt`,
same seam-and-toggle shape as the fake voice pipeline): every
session-related HTTP/SSE call gets a canned, scripted response instead of
hitting the real backend, so on-device overlay/gate testing costs zero LLM
tokens even with auto-listen firing a query on every assist-gesture
invocation. Off by default; toggled via the Settings screen's Debug section
("Use mock backend") or, for automation, the same seed-extra pattern as
`seedBaseUrl`/`seedApiKey`: `adb shell am start -n com.mewbo.aura/.MainActivity
-e mockBackend true`. Scripted content (a happy-path turn, a long-response
variant, an error variant - selectable on-device by query keyword, "long"/
"error") lives in ONE place, `mock/MockScenarios.kt`; its contract test
(`app/src/test/.../mock/MockScenariosTest.kt`) round-trips every scripted
frame through the real `SessionEvent.decode` parser. Launch the gallery
directly: `adb -s <serial> shell am start -n com.mewbo.aura/.ui.aurora.LivenessShowcaseActivity`.

## Package layering — dependencies flow strictly down

```
ui/ (Compose)  →  viewmodels  →  data/ (repos → sse/api → model)
                       ↘  voice/ (Transcriber · Synthesizer seams)
```

- `data/` has **zero Compose/Android-UI imports**; `ui/` never touches
  OkHttp/Retrofit — all I/O goes through repositories.
- All audio/speech code lives in `voice/` behind the `Transcriber` and
  `Synthesizer` interfaces. No app-layer audio code outside `voice/`.
- **A capability advertised to the backend is serviced at the seam EVERY host shares, never at one
  host's ViewModel.** `RunRepository.live()` — the one thing chat and the overlay both go through to
  follow a run — builds `DeviceToolDispatch` into the flow it hands out, so a new entry point can't silently
  re-break it. Advertising it in `sendQuery` while wiring the answer into `ChatViewModel` cost 66.8s
  overlay turns (two 30s server timeouts) once already; the whole story lives in
  `data/CLAUDE.md` § "Device tools" and `voice/CLAUDE.md` § "Device tools from the overlay".
- One chat composable tree (`ChatTranscript`), reused as a COMPONENT by both hosts — never forked.
  `MainActivity` nav is still the ONE app-navigation host; since v5 the assist overlay ALSO
  renders chat again (a `ResponseCard` for the first turn's streaming response, reusing the exact
  same `ChatTranscript` `ChatSurface` does), then hands off to `MainActivity` from the second
  interaction onward. See `ui/overlay/CLAUDE.md` and `voice/CLAUDE.md` for the full v5 contract —
  superseded v4's "overlay never renders chat" rule.

## Turn-completion notifications (`notify/`)

A `dataSync` foreground service follows a backgrounded run to its terminal event and posts a completion
notification (landed 2026-07-14; full mechanics in [`notify/CLAUDE.md`](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md)).
The load-bearing app-level facts:

- **`RunRepository` is now `@Singleton`.** The notification watcher is a THIRD follower of
  `RunRepository.live()` (after chat and the overlay) and must share the ONE multicast SSE connection —
  which only holds if all three share the repo instance. It was safe unscoped before only because the
  `@Singleton DeviceToolExecutor`'s ledger deduped device answers across per-injector repos; a follower
  that wants to SHARE the connection forces a true singleton. Its only state is the session-keyed
  self-evicting `liveStreams` cache (no per-injector state → safe).
- The seam is a `RunNotifications` `fun interface` declared DOWN in `data/repo` and bound to
  `notify/`'s impl in `di/NotifyModule` — dependency flows down, never up. It fires on the three
  run-START paths only.
- The service is a `dataSync` FGS (not `shortService` — that ~3min cap would kill a long turn), started
  at run-start while foregrounded (Android 12+ forbids a background FGS start), and `POST_NOTIFICATIONS`
  is requested at first query-send with NO in-app pre-consent dialog (the OS grant is the sole gate).

## Apps (Mewbo Apps sub-product) — `ui/apps/` + `data/repo/AppRepository`

Native gallery/detail/create screens for the Mewbo Apps sub-product (LLM-built,
trigger-maintained mini apps). The detail screen renders the app's stlite frontend
through the SAME WebView seam widgets use. Load-bearing facts:

- **`AppWebView` reuses `buildStliteWebView` — never a second WebView.** The widget
  card's factory was already payload-agnostic (`buildWidgetWebView` took a
  `messageJson: String`); it was renamed `buildWidgetWebView` → `buildStliteWebView`
  (`private` → `internal`) and reused verbatim, zero logic changed. Its
  ready-signal-gated `postPayloadOnce()` still owns the FIRST payload delivery
  (never `onPageFinished` — the page posts `mewbo-widget-host-ready` when the kernel
  is live).
- **The refresh repost goes through an `AndroidView` `update` block, guarded by the
  WebView's `tag`.** `factory` runs ONCE, so a later `refresh()` (toolbar, or after
  a trigger pause/resume) that mints a new token + `messageJson` had nowhere to go.
  The `tag` doubles as "the payload currently loaded": it is stamped in `factory`
  too, so the FIRST `update` (which `AndroidView` fires immediately after `factory`)
  sees an equal tag and SKIPS — leaving the true first delivery to
  `postPayloadOnce`, never racing it. A LATER recomposition where `messageJson`
  actually differs posts directly via `evaluateJavascript` + restamps the tag; an
  unchanged `messageJson` is a no-op (the host page's `render()` tears down and
  remounts the kernel on every post — a real cost, not free).
- **Theoretical ready-gate race (documented, unreachable in practice).** The
  `update`-block repost bypasses `postPayloadOnce`'s ready-gate, so IN THEORY a
  pre-ready repost is dropped and the ready signal then delivers the stale factory
  payload. Unreachable in practice: the ready signal fires local-asset-fast while a
  refresh needs a human tap + a network round-trip. Left as-is; don't add a second
  gate for it without a real repro.
- **Payload = `mewbo-app-payload`, mirroring the console `host.ts` contract EXACTLY**
  (`{type:"mewbo-app-payload", payload:{entrypoint, files, requirements,
  app_context:{token, api_base, app_id}}, theme}`). Verified field-for-field against
  `apps/mewbo_console/src/widget/host.ts` `parseAppMessage` + `types.ts` `AppContext`
  — a field rename here is a cross-surface break. `token` is the whole `token_id`
  (the bearer credential); `api_base` (NOT `base_url`).
- **`apps` capability advertised UNCONDITIONALLY.** `DataModule`'s `AuthInterceptor`
  sends `X-Mewbo-Capabilities` as a comma-joined list with `apps` always present
  (`stlite` stays gated on `streamlitWidgetsEnabled`) — comma-separated parsing
  verified against `backend.py`. Trigger pause/resume reuses the EXISTING global
  `PATCH /api/triggers/{id}` (triggers stay owned by the trigger subsystem); if the
  API ever ships an app-scoped pause route it is a one-method swap in
  `AppRepository.setTriggerPaused`.
- **`AppRepository` is `@Singleton`**, cache-then-refresh like `SessionRepository`;
  mutations degrade to `null`/`false` on failure. The creation flow follows
  `RunRepository.live(sessionId)` (the shared multicast SSE seam) for
  `SessionEvent.AppReady`, rendering a light "Building…" line, not the full
  `ChatTranscript`. `AppReady` is dropped by `TranscriptReducer` (never a chat item).
- Accepted v1 gap: `app_updated` is not consumed — an open detail screen refreshes
  manually only (mirrors the console + API-side note).

## Atomic class paradigm (Kotlin flavor)

Same rule as the rest of the monorepo: every behavior/feature/strategy is one
atomic class — state as constructor-injected attributes (Hilt), behavior as
methods, pure helpers as `companion object` functions. Sealed interfaces for
closed unions (`SessionEvent`, `OrbState`, `TranscriberEvent`) so `when` is
exhaustive and the compiler flags drift. A free function that grows past two
args or starts carrying state across calls becomes a class. No util-file
dumping grounds.

## Theme discipline

No `Color(`, dp radius, or `spring(` literals outside `ui/theme/`. All
tokens come from `AuraTheme` / `AssistantExtras` (see `ui/CLAUDE.md`).

## Iconography

Icon sourcing has ONE order of preference, settled 2026-07-14 (user directive: never hand-roll an icon
when an off-the-shelf one covers the case):

1. **`material-icons-extended` first** for any NEW glyph (added to the catalog 2026-07-14 — build fact
   below). A semantically-correct extended glyph beats a hand-rolled path every time.
2. **`ui/chat/ChatIcons.kt`'s hand-rolled set is FROZEN LEGACY.** Existing reuses stay (a reused
   hand-rolled glyph is not a "new hand-roll" — `ChatIcons.Clock`/`Fullscreen`/the drawer's archive
   glyph are legitimate legacy reuses), but do NOT add a new hand-rolled path there. The recents-header
   filter icon is the precedent: a hand-ported `filter_alt` path was DELETED in favor of
   `Icons.Default.FilterAlt` once extended landed.
3. **Brand/provider LOGOS have no Material analog, so they ARE hand-converted** — `ModelProviderIcons`
   uses `res/drawable/ic_provider_*.xml` converted from MIT `@lobehub/icons` MONOCHROME SVGs (single
   `currentColor` path → tintable, never the colored variants, per theme discipline). Each drawable
   carries its upstream-source + license as an XML provenance comment.

Two standing rules: **one icon weight per surface** (the drawer/settings are all `Icons.Filled.*`, so a
new drawer glyph is Filled — don't mix Outlined in); and the ephemeral **Temporary project uses
`Schedule` (a clock), NOT an `AutoDelete`/trash glyph** — a delete-affordance glyph next to a
selectable, tappable row misreads as "tap to delete" (the delete-affordance trap).

## Testing

`ANDROID_HOME=$HOME/android-sdk ./gradlew :app:testPublicDebugUnitTest` (test
sources are flavor-agnostic; `testDebugUnitTest` no longer exists under the
distribution flavors — pick either flavor) — the
reducer (`TranscriptReducer`), `SentenceChunker`, and `AssistTurnMachine` suites are the contract
tests; any serialization/event change must keep them green. Tests exercise
real code paths (real JSON fixtures from the live API shape), stubbing only
transport. See `app/src/test/java/com/mewbo/aura/CLAUDE.md` for JVM-test-only house idioms
(coroutine scopes, scripted fakes, timestamp parsing) before writing a new test double.
