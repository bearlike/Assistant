> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [data](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) · [ui](app/src/main/java/com/mewbo/aura/ui/CLAUDE.md) · [voice](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md) · [notify](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md) · [di](app/src/main/java/com/mewbo/aura/di/CLAUDE.md) · [mock](app/src/main/java/com/mewbo/aura/mock/CLAUDE.md) · [debug](app/src/debug/java/com/mewbo/aura/CLAUDE.md) · [test](app/src/test/java/com/mewbo/aura/CLAUDE.md) · [redroid](tools/redroid/CLAUDE.md)

# Mewbo Aura — Android Assistant Client

Scope: `apps/mewbo_aura/` — native Kotlin + Jetpack Compose assistant app (assist overlay via the
system assistant role + sessions/chat UI) over the Mewbo session REST+SSE API. The backend owns all
logic; Aura is a thin frontend.

**Design system: [`DESIGN.md`](DESIGN.md) is canonical for visual laws, token values, provenance, and
the regression registry ("never again").** Read it before ANY visual/UI change; when a scoped
CLAUDE.md and DESIGN.md disagree, DESIGN.md wins and the CLAUDE.md is stale. in these
files are DESIGN.md's provenance shorthand (user-directive rounds, in supersession order).

## CLAUDE.md tree (this app)

Read the deepest file that applies before editing; a new substantive package gets one.

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
| Data · screen control at shell UID (Shizuku bind, pruning, settle, geometry) | `app/src/main/java/com/mewbo/aura/data/device/shizuku/CLAUDE.md` |
| Data · settings (`SettingsStore` keys, `KeystoreCipher`) | `app/src/main/java/com/mewbo/aura/data/settings/CLAUDE.md` |
| Data · in-app updater (one client for both forges, asset nomenclature, the signature trap) | `app/src/main/java/com/mewbo/aura/data/update/CLAUDE.md` |
| **UI — hub**: thin router, Compose stability, one-chat-tree, a11y | `app/src/main/java/com/mewbo/aura/ui/CLAUDE.md` |
| UI · theme (tokens, discipline, `AssistantExtras`, reduced-motion) | `app/src/main/java/com/mewbo/aura/ui/theme/CLAUDE.md` |
| UI · orb + shared shader primitives (`GlslNoise`/`ClayFlowerSdf`) | `app/src/main/java/com/mewbo/aura/ui/orb/CLAUDE.md` |
| UI · shader family (`AuroraEdgeGlow`/`AuroraWashTop`) | `app/src/main/java/com/mewbo/aura/ui/aurora/CLAUDE.md` |
| UI · chat surface (`ChatScreen`/`Transcript`, message actions, scope row) | `app/src/main/java/com/mewbo/aura/ui/chat/CLAUDE.md` |
| UI · promoted-tool action cards (`PromotedTools` allowlist trap) | `app/src/main/java/com/mewbo/aura/ui/chat/toolcards/CLAUDE.md` |
| UI · Streamlit widget card (WebView ready-signal) | `app/src/main/java/com/mewbo/aura/ui/chat/widget/CLAUDE.md` |
| UI · composer (`AuraComposer`, `RmsWaveform`, scope-row anchor) | `app/src/main/java/com/mewbo/aura/ui/composer/CLAUDE.md` |
| UI · assist overlay render (`AssistUiState`) | `app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md` |
| UI · Mewbo Apps screens (`AppWebView`'s two payload doors, the fixed envelope) | `app/src/main/java/com/mewbo/aura/ui/apps/CLAUDE.md` |
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

This directory is its own Gradle root (`settings.gradle.kts` lives HERE, not at the repo root).
Nothing Android leaks upward: no root-level gradle files, caches, or SDK paths.

- Build entry point is ONLY `./gradlew` from this directory. No system Gradle.
- `ANDROID_HOME` is not exported in CI/agent shells — prefix commands:
  `ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug`.
- `local.properties` (gitignored) carries `sdk.dir` for IDE use.
- Repo-wide Python tooling (ruff/mypy/pytest) ignores this subtree; Kotlin quality gates run
  through Gradle only.

## Build → install → verify loop (mandatory after UI/data changes)

```bash
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assembleEnterpriseDebug
adb -s localhost:5555 install -r app/build/outputs/apk/enterprise/debug/aura-*-enterprise-debug.apk
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity
adb -s localhost:5555 exec-out screencap -p > /tmp/aura.png   # then LOOK at it
adb -s localhost:5555 logcat -d --pid=$(adb -s localhost:5555 shell pidof com.mewbo.aura) | grep -E "FATAL|AndroidRuntime"
```

**Every adb/gradle invocation passes `-s <serial>`** — never default device selection; redroid and
the Pixel can be connected simultaneously.

**Distribution flavors (`public` · `enterprise`).** `enterpriseDebug` is the dev/enterprise variant —
it bakes a private, deployment-supplied root CA as a Network Security Config trust anchor and permits
cleartext for LAN backends, so the loop above builds `assembleEnterpriseDebug`. `public*` carries
platform trust only (system CAs) and is the ONLY flavor CI builds + ships publicly. The umbrella
`assembleDebug`/`assembleRelease` build BOTH flavors — always name the flavor. The CA cert is
gitignored and auto-seeded from `~/temp_folder/enterprise-ca.crt` by the `seedEnterpriseCa` task
(override with `-Pmewbo.enterpriseCaSource=` / `AURA_ENTERPRISE_CA_SRC` / `AURA_ENTERPRISE_CA_B64`);
an enterprise build fails loudly if it can't find the cert. **Never again:** `public*` APKs must not
contain the CA — verify with `unzip -l <apk> | grep enterprise_ca` (public = empty, enterprise =
`res/raw/enterprise_ca.crt`).

### 🚨 A RELEASE APK IS `enterpriseDebug`. ALWAYS. Verify the artifact before attaching it.

**The gate, and it is not optional — run it on the file you are about to upload:**

```bash
unzip -l <apk> | grep enterprise_ca   # MUST print res/raw/enterprise_ca.crt
```

CI builds and attaches `enterpriseDebug` on the self-hosted forge. A hand-built local attach is the
fallback; run the gate above on every APK attached that way.

**The asset name is produced BY THE BUILD and must be uploaded unchanged:
`aura-<versionName>-<flavor>-<buildType>.apk`** (`aura-0.0.20-enterprise-debug.apk`). The in-app
updater picks a release's asset by matching the `-<flavor>-<buildType>.apk` SUFFIX, so a renamed
upload is invisible to every device — the update simply never appears, silently, with the release
looking perfectly fine on the forge. **A release asset whose suffix is not `-enterprise-debug.apk`
is the wrong flavor**, and the suffix check is the same one that used to be "compare against the
previous release's asset name". Releases published before the scheme carry `app-enterprise-debug.apk`
and stay visible, because that name ends the same way; do not rename them.

**Tag the release `aura-<version>` — the prefix is load-bearing, not decoration.** The repository's
tags are shared with the server's own releases, and the updater drops every tag that does not start
with `aura-`. Scheme and rationale: [`data/update/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/update/CLAUDE.md).

**An enterprise build now also needs its in-app update API root**, on the same argument chain as the
CA: `~/temp_folder/aura-update-api-root.txt`, `-Pmewbo.updateApiRoot=<url>`, or
`AURA_UPDATE_API_ROOT`. It is the release API ROOT with a trailing slash (`…/api/v1/` for
Gitea/Forgejo), never a repository URL, and it never appears in a tracked file. Absent, the
enterprise build FAILS at `requireEnterpriseUpdateApiRoot` rather than shipping an APK that cannot
find its own updates. `public` builds default to `https://api.github.com/` and need nothing.

**Self-update chains across machines only when the signing keystore is configured.**
`app/build.gradle.kts` gives both build types its `release` signing config: when `AURA_KEYSTORE_B64`
and its credentials are set, every variant uses that stable key. When they are unset,
`enterpriseDebug` falls back to the machine-local auto-generated debug keystore; Android then
refuses an update across a signature change, and the cure is an uninstall that loses the user's
data. [`data/update/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/update/CLAUDE.md) § "Signature
reality" carries the detail.

**Why this needs a hard gate rather than a note: every signal says the build succeeded.** A
`publicDebug` APK compiles, installs, launches, is the same size to within 0.3%, and contains every
line of new code — it is a *correct build of the wrong flavor*. It fails only later, on the device,
against the LAN backend, as
`CertPathValidatorException: Trust anchor for certification path not found`. Nothing in the build,
the test suite, or a dex inspection distinguishes the two; the ONLY distinguishing artifact is
`res/raw/enterprise_ca.crt`.

**This has shipped wrong.** Two prereleases in a row were built with `assemblePublicDebug` and
attached, by someone who had read this very section — the habit of typing the flavor that unit tests
use (`testPublicDebugUnitTest`, which is correct, since test sources are flavor-agnostic) carried
straight into the assemble command. **`testPublicDebug*` for tests and `assembleEnterpriseDebug` for
the artifact is not a contradiction — it is the rule.**

**Before any local `assemble*Release`: build the console first** (`npm run build` in
`apps/mewbo_console`, which also emits `dist/widget-host/`). CI builds that bundle before assembling
its artifacts. A RELEASE build FAILS HARD when it is absent (`syncWidgetHostAssets<Variant>` — a
release must never silently ship the widget feature advertised-but-broken); debug builds only warn,
so an `enterpriseDebug` artifact can otherwise publish with its widget renderer absent.

## Device matrix

| Tier | Device | Use for | Hard limits |
|---|---|---|---|
| 1 | redroid container (`tools/redroid/`, `localhost:5555`) | UI, chat, streaming, reducer work | **Any code behind an `isEmulator` check is UNREACHABLE here by construction** — a real-hardware-only branch cannot be witnessed on this tier at all, and a launch crash living in one shipped because every gate was green (see `data/settings/CLAUDE.md` § DataStore ownership). Force the predicate false in a throwaway build to exercise it. AOSP: **no `SpeechRecognizer`, no TTS engine** → always `FakeTranscriber`/`FakeSynthesizer`; software GPU → FPS numbers meaningless; assistant role/gesture not representative; **`privileged: true`, so it is NOT a valid witness for anything gated on real shell-UID enforcement** (screen control) |
| 1b | redroid rebooted at TV geometry (see `tools/redroid/CLAUDE.md`) | **D-pad focus traversal at 16:9** — `input keyevent 19–23` + Compose `assertIsFocused` | **no `android.software.leanback`** and no TV launcher: cannot witness store filtering, banner/tile presentation, or anything reading `hasSystemFeature(FEATURE_LEANBACK)` |
| 2 | physical Pixel over Wi-Fi adb | assistant role, gesture overlay, real STT/TTS, haptics, final acceptance | needs a human in the loop |
| 3 | Google TV emulator (`system-images;android-*;google-tv;x86_64`) | leanback gating, launcher/banner presentation | needs `/dev/kvm`; UNVERIFIED on this host |
| 4 | physical Fire TV Stick over network ADB | Fire OS divergence, real remote, final TV acceptance | manual only; the ONLY witness for Fire OS |

## TV-shape facts — don't re-derive

- **D-pad works on the AOSP tier today.** `KEYCODE_DPAD_*` is dispatched by the input framework and
  Compose focus responds to key events — neither depends on `characteristics=tv` or the leanback
  feature. So redroid validates remote traversal unmodified; only the cheap questions (store
  filtering, launcher tile) need a real TV image.
- **redroid CANNOT be made into an Android TV.** `/system` is read-only; the leanback feature is a
  permissions XML baked at image build. A custom image is the only lever and is not worth it.
- **The floor is `minSdk 30` (Android 11), and `RuntimeShader` — not haptics — is what made that
  expensive.** Dropping from 33 was driven by a real Android TV that reports API 30; below the old
  floor the package manager refuses the APK before any manifest feature or launcher category
  matters. Measured by setting the floor and reading lint rather than grepping: **67 `NewApi`
  errors, 63 of them `android.graphics.RuntimeShader`, which is API 33.** The whole AGSL family
  (orb, spark, both aurora surfaces) was gated on the exact level we were leaving. `VibratorManager`
  (API 31) was only 4 of the 67.
  - **`AuraShaders.supported` is the ONE gate** (`ui/orb/`, beside the other shared shader
    primitives). Every AGSL surface asks it once and early-returns to a plain-Compose fallback;
    the shader paths carry `@RequiresApi(TIRAMISU)` so lint PROVES the gate. Never a
    `@SuppressLint("NewApi")` and never a lint baseline — both hide a crash on hardware nobody in
    the loop is holding.
  - **The `reducedMotion` paths are NOT that fallback** and cannot be reused as one: they render
    the same shader frozen. The API 30-32 fallbacks are separate, deliberately modest, and drop
    motion fidelity entirely — this surface is a television where the orb is decorative.
  - `VibratorManager` resolution branches in `data/device/VibratorResolver` (see `di/CLAUDE.md`).
  - compileSdk 37 is a COMPILE floor forced by the AAR set and is unrelated to the runtime floor.
- **A `checkSelfPermission` read of a permission that post-dates the floor reports a fiction.**
  `POST_NOTIFICATIONS` is only runtime-enforced from API 33; below that it reads GRANTED
  unconditionally, including for a user who switched notifications off. Settings therefore reads
  `NotificationManagerCompat.areNotificationsEnabled()` via `NotificationPermissionReader`, which is
  correct on both sides of the split. Any future permission gated above the floor inherits this trap.
- **The app was unnavigable by remote, and it was NOT because things were unfocusable.** Measured at
  1920×1080/320dpi (= 960×540dp): the accessibility tree already held eight focusable nodes, because
  `clickable`/`combinedClickable` ARE focusable in Compose. Focus was TRAPPED, not absent — a focused
  Compose text field consumes all four arrows to move its caret, the composer takes focus on the
  first frame, and eight consecutive arrow presses never moved focus once. **Adding
  `Modifier.focusable` anywhere would have changed nothing.** Three fixes, all in `ui/common/`:
  - `TextFieldFocusEscape` + `Modifier.dpadFocusEscape` — Up/Down always leave a text field;
    Left/Right leave only from a collapsed caret at the text's boundary, so in-text editing survives.
  - `Modifier.auraFocusRing` — the one visible focus state. **It must come BEFORE the click modifier
    in the chain.** `onFocusChanged` observes only focus targets that FOLLOW it, so ringing from
    behind compiles, draws nothing, and warns about nothing — measured as a drawer that held focus
    correctly and rendered no ring at all. A Material `IconButton`/`Switch`/`Button` applies its own
    click after its `modifier`, so passing the ring as that component's `modifier` is already right;
    the trap is only on a hand-built `Box`/`Row` where you append.
  - Each surface sets its own initial focus with a `FocusRequester`, or the opening presses land
    nowhere.
- **`focusProperties { down = FocusRequester.Cancel }` on the composer is load-bearing, not tidiness.**
  The composer is bottom-most, so Down has no target — and Compose does not merely fail that move: it
  moved focus to a node the IME's reflow then destroyed, after which the tree reported NO focused node
  in any direction, permanently. A handheld recovers because a finger grants focus; a remote has no
  such gesture, so the app was unrecoverable short of force-stopping it. **Recovering after the fact
  is impossible** — Compose dispatches no key event at all once focus is gone, so a root
  `onPreviewKeyEvent` never fires (tried, measured, does not work). Containment is the only cure.
- **A D-pad centre LONG-PRESS already works — do not build overflow menus for it.**
  `combinedClickable` handles it, so long-press-only actions (session Rename/Archive, message
  actions) are reachable on a remote with zero code. Verified with `input keyevent --longpress 23`:
  the actions sheet opened and its rows traverse normally.
- **`adb shell input tap` cannot verify any of this.** A tap puts the window in touch mode, where
  Android refuses focus outright — an initial-focus `requestFocus` that works perfectly from a remote
  reads as "nothing focused" if you drove it with a tap. Drive focus tests with `input keyevent` only.
- **A trailing `Switch` inside a row is focusable and still unreachable** — measured in all four
  directions. Settings rows therefore carry the click themselves and render the switch with
  `onCheckedChange = null` as a display-only indicator; a disabled switch's row must stay
  non-clickable, or a remote could toggle what the touch UI refuses.
- **The two device shapes are named in one place, `DeviceShape` (`data/device/DeviceShape.kt`),
  resolved once via `DeviceShape.of(TelevisionChecker)` and published app-wide as `LocalDeviceShape`
  (`MainActivity`, `staticCompositionLocalOf`).** Differences are MEMBERS
  (`opensKeyboardOnFocus`, `hasOverlayPermissionScreen`), never a boolean re-asked at each reader — a
  boolean asked at N call sites is N independent chances to get a third shape wrong, and adding one
  here is a compile error at every arm that has not answered the new question. The one legitimate
  `when` on it picks between two whole composable trees (`AuraNavHost`'s `ChatHomeDestination`);
  every other reader asks a member, never a `when`. **A candidate third member,
  `speaksRepliesByDefault`, was tried and deliberately reverted** — `SettingsStore.speakResponses`
  stays `true` on every shape (`data/settings/SettingsStore.kt`'s own KDoc): the read-aloud default
  was already `true` everywhere, so making it device-conditional would have changed nothing on
  television while silently flipping it OFF for every handheld that had never touched the switch.
  What actually left a television silent was which SYNTHESIZER it resolved to, not this flag — see
  the fallback below. Not every plausible member belongs on the sealed interface; one that would
  regress the other shape does not.
- **A permanent navigation rail replaces the modal drawer on television — not a patched variant of
  it.** `ChatHomeDestination`'s `when` renders one of two shells: `HandheldChatHome` (today's
  `ModalNavigationDrawer`, unchanged) or `TelevisionChatHome` (a fixed-width
  `AuraSpacing.NavigationRail.width` rail, mounting `AuraDrawerContent` verbatim with
  `NavigationHost.PersistentRail`). Making the drawer itself remote-operable was the shape tried
  first and rejected: a modal sheet has to be summoned (no summoning gesture on a remote), it lands
  over content behind a scrim (nothing to see through on a television), and it needs focus
  containment that flips direction between open and closed — none of which a rail on permanent
  screen real estate needs at all. `NavigationHost` (`ModalSheet(isOpen)` / `PersistentRail`) carries
  the two fields that actually differ (`isActive`, `containsFocus`) so `AuraDrawerContent` renders
  the same rows either way and never asks which shell it is in.
- **`LocalIsTelevision` has been fully retired by `LocalDeviceShape` — deleted, not merely
  superseded.** `ImeOnConfirmOnly.kt` now reads `LocalDeviceShape.current.opensKeyboardOnFocus` (below) same
  as every other reader, so there is exactly one device-shape seam left in the UI layer. (This
  consolidation landed mid-wave, after an earlier pass through this app documented the two locals as
  coexisting — if a stale reference to `LocalIsTelevision` turns up anywhere, the code has moved on
  and the doc is what's behind.)
- **Focusing a text field on television must not raise the keyboard.**
  `Modifier.imeOnConfirmOnly()` (`ui/common/ImeOnConfirmOnly.kt`) hides the keyboard on focus and
  opens it only on an explicit confirm (`DirectionCenter`/`Enter`/`NumPadEnter`) — D-pad traversal
  moves focus THROUGH a field on the way past it, and Compose's default (raise-on-focus) puts a
  full-screen IME over a user who was only navigating; BACK then dismisses the IME instead of moving
  focus, so the remote oscillates and never gets past the field. On a handheld the modifier returns
  the receiver completely unchanged — no focus observer, no key handler added — because
  `DeviceShape.Handheld.opensKeyboardOnFocus` is `true`. **Named for the BEHAVIOUR, not the device**,
  so a call site picks "ask before typing" and which shapes need that stays the seam's answer; a
  `tv`-prefixed name reads as television plumbing and invites a second, device-shaped gate beside it.
  Applied beside `dpadFocusEscape()` on every text field in the app, not only the composer.
- **A sideloaded app cannot receive the assistant button on ANY TV.** Android TV/Google TV has no
  path to the assistant role (`VoiceInteractionService` is coupled to `RecognitionService` — Google
  confirmed intentional; Home Assistant's client hit the same wall). Fire TV binds the button to
  Alexa; its integration surfaces (Video Skill API, custom skills) are cloud-side intents, not keys.
  On TV, Aura is an app you OPEN, not an assistant role — the overlay's invocation model has no
  trigger there and must be hidden via `hasSystemFeature(FEATURE_LEANBACK)`.
- **One app, not a second.** Same-module adaptation (manifest + focus + tokens) beats a second
  `tv` module: the one-chat-tree law (`ChatTranscript`) is what keeps the two shapes from drifting.
  `androidx.tv:tv-material` must NOT be mixed with `compose.material3` (separate `MaterialTheme`
  objects) — do not add it unless focus-indicator quality fails review.
- **Layout assertions on the JVM layer need `@GraphicsMode(GraphicsMode.Mode.NATIVE)`** — under the
  LEGACY default `Paint.measureText` is `text.length()`, so a "does this fit at 960dp" comparison
  passes with zero power to fail. Robolectric accepts TV qualifiers (`w960dp-h540dp-television-xhdpi`);
  Paparazzi has no TV preset — build one via `DeviceConfig.copy(uiMode = UiMode.TELEVISION, ...)`.
- **The plan lives in the tracker** (`🚧 Aura on the television`, labels `Area/Android` +
  `type/🚧 spec`) with the phased checklist; the storefront-filter numbers there are read from
  published rules, never from a submission.

Container ops (binder module load, subnet pin, `data/` wipe, backend line-of-sight, GMS variant) live
in [`tools/redroid/CLAUDE.md`](tools/redroid/CLAUDE.md) — read it before touching the container.

## Hard-won build facts (don't re-derive)

- AGP 9.2.1 has **built-in Kotlin** — applying `org.jetbrains.kotlin.android` hard-errors. Its
  internal Kotlin is pinned to 2.4.0 via `buildscript classpath` in the root build file to match the
  compose/serialization plugins; jvmTarget lives in top-level `kotlin { compilerOptions {} }`.
- compileSdk 37 is forced by AAR floors (lifecycle 2.11, hilt-nav-compose 1.4, mikepenz markdown
  0.43) — don't "fix" it back to 36.
- KSP 2.3.9: Hilt modules/`@Provides` must never be `internal` (upstream codegen break).
- dagger 2.60's POM omits `error_prone_annotations` but its ViewModel-component codegen references
  it — the catalog carries the dep explicitly; removing it breaks the first `@HiltViewModel`.
- material3 1.4.0's `MaterialExpressiveTheme`/`MotionScheme` are `internal` (unreachable, and `javap`
  misleadingly shows them public — Kotlin visibility is frontend-enforced). Plain `MaterialTheme` +
  `AuraMotion` tokens is the settled approach, not a stopgap.
- Gradle 9.6 removed `project.exec` — build-script subprocess calls use `ProcessBuilder` (see the
  release-signing keytool bootstrap).
- `android.net.Uri` can't be hand-doubled in plain-JVM unit tests (package-private ctor, real methods
  throw in the stub) — the catalog carries `mockito-core` solely for tests needing a behaving `Uri`.
  Usage + other JVM-test-only traps: `app/src/test/java/com/mewbo/aura/CLAUDE.md`.
- `androidx.compose.material:material-icons-extended` is BOM-managed by the compose BOM → 1.7.8. R8
  strips the unused glyphs in `release`; `debug` carries the full aar. Its glyphs DEPEND on
  `material-icons-core`, so a jar grep for a CORE glyph (`Build`, `Settings`) inside the extended aar
  returns 0 — expected, not a missing dependency.

## Shizuku on the container — the dev loop for screen control

Everything under `data/device/shizuku/` needs a running Shizuku server. Four facts, each of which
cost real time to find:

- **`shizuku_starter` refuses to run under a rooted adbd, and the failure reads as a no-op.**
  `fatal: run Shizuku from non root nor adb user (uid=%d)` — so if you ran `adb root` earlier (to
  read `/proc/net/tcp` for another uid, say), the restart silently does nothing. `adb unroot` first:

  ```sh
  adb unroot
  /data/local/tmp/shizuku_starter --apk=$(pm path moe.shizuku.privileged.api | cut -d: -f2)
  ```

- **`shell` is Shizuku's NORMAL mode and the tier the device tools target.** A server running as
  ROOT is the unusual case, not a healthier one — and root actively breaks reproduction of
  permission-staged failures, because it bypasses file modes. A mode-400 trap silently does nothing,
  the capture "succeeds", and a test reports a false green. To exercise a failure path through a
  root service, stage a root-proof one instead (make the capture path a directory).
- **Verify a restart by CAPABILITY, not by process existence.** A live `shizuku_server` does not
  prove the app re-derived its grant. Force-stop Aura, relaunch, and check the composer reads the
  full device-tool count (11, with the control + lifecycle tools).
- **`Shizuku.checkSelfPermission()` asks the Shizuku SERVER, not `PackageManager`.** `pm grant` reads
  back as granted while the server still refuses — the OS flag and the authorisation are different
  facts, and only the second is the gate. So the remedy names Shizuku's own UI, never a permission
  screen.

## Recurring-401 rule + credential seeding

A 401 from the app on the dev device almost always means **the app's stored API key is gone**
(container recreated, `data/` wiped, or a fresh install) — NOT a backend/auth bug. Don't
re-diagnose; re-seed deterministically (debug builds only):

```bash
adb -s localhost:5555 shell am start -n com.mewbo.aura/.MainActivity \
  -e seedBaseUrl http://api:5125 \
  -e seedApiKey "$(grep '^MEWBO_MASTER_API_TOKEN' "$(git rev-parse --show-toplevel)/.env" | cut -d= -f2)"
```

Never hand-type the key through the Settings UI in automation — `adb input text` mangles long secrets.

## Parallel agents in one worktree

Multiple implementer agents share this working tree concurrently. Three mechanisms have destroyed or
misattributed work here:

- `git checkout -- <foreign-path>` reverts to the last COMMIT, destroying any OTHER agent's
  uncommitted edits to that path — not just your own.
- A tree-wide `git stash` (even "isolated verify, restore right after") opens a window where every
  other agent's `ls`/`Read` sees the pre-stash tree — reads as a false "work lost" alarm.
- A bare `git add <your files>` + bare `git commit` commits the WHOLE shared index, including any
  OTHER agent's already-staged, not-yet-committed files.

Rules: never run `checkout --`/`clean`/`reset`/`stash` on a path outside your own lane; commit with
an explicit pathspec (`git commit -m "..." -- <exact paths>`) or inspect `git diff --cached --stat`
first; commit your own coherent slice early rather than waiting for a whole-tree green. Verify tree
state with `git diff`/`git status`, never a bare `ls`/`grep`/`Read` — those can return stale reads
mid-churn. Gradle's `UP-TO-DATE` is untrustworthy here too — force `--rerun` on at least one
verification pass before trusting a result.

Run `:app:lintPublicDebug` at every wave boundary, not just at the end — a missing `VIBRATE`
permission silently no-op'd every haptic because lint never ran until the final gate. (`lintDebug`
is AMBIGUOUS under the distribution flavors; name the flavor.)

**⚠️ `app/build/test-results/` is SHARED, so a concurrent run's report can be sitting where yours
should be.** A task-level `BUILD FAILED` next to a green XML does not mean "trust the XML" — it
means the XML is probably not yours. Two checks that work, and one that does not:

- **COUNT.** A `--tests`-filtered run cannot produce the full-suite number. A filtered task
  reporting the whole suite is reading someone else's run.
- **MTIME.** `ls --time-style=+%H:%M:%S` the XMLs; anything postdating your invocation is not
  yours. This is the ONLY check available to an UNFILTERED run.
- **Membership does NOT work for an unfiltered run.** "Are the classes I expect present" is
  equally true of your build and of any concurrent full-suite build — a test with no power to
  fail. Measured: a "verified" full-suite number came from a directory that had been rewritten
  twice mid-check.

Same cause, all transient and green on retry: `[ksp] FileNotFoundException` on a generated Hilt
file, `EOFException` on `in-progress-results-generic.bin`, and a run collecting a fraction of the
suite. **Re-run before believing a red whose message names a file you did not touch.**

## Debug-variant tooling (src/debug)

Debug builds ship three preview hosts (`LivenessShowcaseActivity`, `ChatPreviewActivity`,
`AssistOverlayPreviewActivity`), the fake voice pipeline, and the **mock backend** — a debug-only
`OkHttpClient` interceptor giving every session HTTP/SSE call a canned scripted response, so
on-device overlay/gate testing costs zero LLM tokens even with auto-listen firing a query on every
invocation. Full inventory: [`debug/CLAUDE.md`](app/src/debug/java/com/mewbo/aura/CLAUDE.md); mock
mechanics: [`mock/CLAUDE.md`](app/src/main/java/com/mewbo/aura/mock/CLAUDE.md).

```bash
adb -s <serial> shell am start -n com.mewbo.aura/.ui.aurora.LivenessShowcaseActivity
adb -s <serial> shell am start -n com.mewbo.aura/.MainActivity -e mockBackend true
```

## Package layering — dependencies flow strictly down

```
ui/ (Compose)  →  viewmodels  →  data/ (repos → sse/api → model)
                       ↘  voice/ (Transcriber · Synthesizer seams)
```

- `data/` has **zero Compose/Android-UI imports**; `ui/` never touches OkHttp/Retrofit — all I/O
  goes through repositories.
- All audio/speech code lives in `voice/` behind the `Transcriber` and `Synthesizer` interfaces. No
  app-layer audio code outside `voice/`.
- **A capability advertised to the backend is serviced at the seam EVERY host shares, never at one
  host's ViewModel.** `RunRepository.live()` builds `DeviceToolDispatch` into the flow it hands out,
  so a new entry point can't silently re-break it. Advertising in `sendQuery` while wiring the answer
  into `ChatViewModel` cost 66.8s overlay turns (two 30s server timeouts) once. Mechanics:
  [`data/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) § "Device tools".
- **One chat composable tree (`ChatTranscript`), reused as a COMPONENT by both hosts — never
  forked.** `MainActivity` nav is the ONE app-navigation host; the assist overlay ALSO renders chat
  (a `ResponseCard` for the first turn's streaming response, the same `ChatTranscript` `ChatSurface`
  uses), then hands off to `MainActivity` from the second interaction onward. Contract:
  [`ui/overlay/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md) and
  [`voice/CLAUDE.md`](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md).

## Turn-completion notifications (`notify/`)

A `dataSync` foreground service follows a backgrounded run to its terminal event and posts a
completion notification (mechanics in
[`notify/CLAUDE.md`](app/src/main/java/com/mewbo/aura/notify/CLAUDE.md)). App-level consequence:
**`RunRepository` is `@Singleton`** — the watcher is a THIRD follower of `RunRepository.live()`
(after chat and the overlay) and must share the ONE multicast SSE connection, which only holds if
all three share the repo instance.

## Apps (Mewbo Apps sub-product) — `ui/apps/` + `data/repo/AppRepository`

Native gallery/detail/create screens for the Mewbo Apps sub-product (LLM-built, trigger-maintained
mini apps). The detail screen renders the app's stlite frontend through the SAME WebView seam widgets
use.

- **`AppWebView` reuses `buildStliteWebView` — never a second WebView.** The widget card's factory
  was already payload-agnostic (it takes a `messageJson: String`) and is reused verbatim. Its
  ready-signal-gated `postPayloadOnce()` still owns the FIRST payload delivery (never
  `onPageFinished` — the page posts `mewbo-widget-host-ready` when the kernel is live).
- **The refresh repost goes through an `AndroidView` `update` block, guarded by the WebView's
  `tag`.** `factory` runs ONCE, so a later `refresh()` (toolbar, or after a trigger pause/resume)
  that mints a new token + `messageJson` had nowhere to go. The `tag` doubles as "the payload
  currently loaded": it is stamped in `factory` too, so the FIRST `update` (which `AndroidView` fires
  immediately after `factory`) sees an equal tag and SKIPS — leaving the true first delivery to
  `postPayloadOnce()`, never racing it. A LATER recomposition where `messageJson` actually differs
  posts via `evaluateJavascript` + restamps the tag; an unchanged `messageJson` is a no-op (the host
  page's `render()` tears down and remounts the kernel on every post — a real cost, not free).
- **Theoretical ready-gate race (documented, unreachable in practice).** The `update`-block repost
  bypasses `postPayloadOnce()`'s ready-gate, so IN THEORY a pre-ready repost is dropped and the ready
  signal then delivers the stale factory payload. Unreachable in practice: the ready signal fires
  local-asset-fast while a refresh needs a human tap + a network round-trip. Don't add a second gate
  without a real repro.
- **Payload = `mewbo-app-payload`, mirroring the console `host.ts` contract EXACTLY**
  (`{type:"mewbo-app-payload", payload:{entrypoint, files, requirements, app_context:{token,
  api_base, app_id}}, theme}`). Verified field-for-field against
  `apps/mewbo_console/src/widget/host.ts` `parseAppMessage` + `types.ts` `AppContext` — a field
  rename here is a cross-surface break. `token` is the whole `token_id` (the bearer credential);
  `api_base` (NOT `base_url`).
- **`apps` capability advertised UNCONDITIONALLY.** `DataModule`'s `AuthInterceptor` sends
  `X-Mewbo-Capabilities` as a comma-joined list with `apps` always present (`stlite` stays gated on
  `streamlitWidgetsEnabled`) — comma-separated parsing verified against `backend.py`. Trigger
  pause/resume reuses the EXISTING global `PATCH /api/triggers/{id}`; if the API ever ships an
  app-scoped pause route it is a one-method swap in `AppRepository.setTriggerPaused`.
- **`AppRepository` is `@Singleton`**, cache-then-refresh like `SessionRepository`; mutations degrade
  to `null`/`false` on failure. The creation flow follows `RunRepository.live(sessionId)` for
  `SessionEvent.AppReady`, rendering a light "Building…" line, not the full `ChatTranscript`.
  `AppReady` is dropped by `TranscriptReducer` (never a chat item).
- Accepted gap: `app_updated` is not consumed — an open detail screen refreshes manually only
  (mirrors the console + API side).

Screen-level mechanics (the `factory`/`update` tag guard, and why a renamed payload field fails
SILENTLY rather than loudly) live in
[`ui/apps/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/apps/CLAUDE.md) — read it before touching
`AppWebView`.

## Project context — a session's directory MOVES mid-run

A session can run in auto workspace mode (`context.project == "auto"`), where the model picks the
project itself via `list_projects` / `switch_project` and may switch again later. Consequences for
this client (full detail in
[`data/model/CLAUDE.md`](app/src/main/java/com/mewbo/aura/data/model/CLAUDE.md)):

- **`lastContextProject` is live state, not a hydration-time read** — a `switch_project` call
  persists a fresh `context` event, so the project is folded per event rather than scanned once.
- **⚠️ Use `as? JsonPrimitive`, NEVER the `jsonPrimitive` accessor, on this path.** The accessor
  THROWS on a nested object or array; on the live per-event path that takes the run's collector down
  with it.
- **`isContextEvent` exists because a bare `String?` collapses two facts** — "a context event naming
  no project" (session on a temp directory) versus "not a context event at all" (nothing to adopt).
- **The switch is a promoted tool card**, so BOTH halves of the allowlist ship together —
  `PromotedTools.IDS` and the `ToolCardRegistry` branch
  ([`ui/chat/toolcards/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/chat/toolcards/CLAUDE.md)).
- **The result is JSON with fixed keys, not prose** — parse it, never regex the sentence. A REFUSED
  switch arrives as the shared `{"error": {code, message}}` envelope and must not render as a
  successful move; anything unparseable degrades to `GenericToolCard`.

## Atomic class paradigm (Kotlin flavor)

Same rule as the rest of the monorepo: every behavior/feature/strategy is one atomic class — state as
constructor-injected attributes (Hilt), behavior as methods, pure helpers as `companion object`
functions. Sealed interfaces for closed unions (`SessionEvent`, `OrbState`, `TranscriberEvent`) so
`when` is exhaustive and the compiler flags drift. A free function that grows past two args or starts
carrying state across calls becomes a class. No util-file dumping grounds.

## Theme discipline

No `Color(`, dp radius, or `spring(` literals outside `ui/theme/`. All tokens come from `AuraTheme` /
`AssistantExtras` (see [`ui/theme/CLAUDE.md`](app/src/main/java/com/mewbo/aura/ui/theme/CLAUDE.md)).

## Iconography

Icon sourcing has ONE order of preference — never hand-roll an icon when an off-the-shelf one covers
the case:

1. **`material-icons-extended` first** for any NEW glyph. A semantically-correct extended glyph beats
  a hand-rolled path every time.
2. **`ui/chat/ChatIcons.kt`'s hand-rolled set is FROZEN LEGACY.** Existing reuses stay (a reused
  hand-rolled glyph is not a "new hand-roll" — `ChatIcons.Clock`/`Fullscreen`/the drawer's archive
  glyph are legitimate legacy reuses), but do NOT add a new hand-rolled path there. Precedent: the
  recents-header filter icon's hand-ported `filter_alt` path was DELETED in favor of
  `Icons.Default.FilterAlt`.
3. **Brand/provider LOGOS have no Material analog, so they ARE hand-converted** —
  `ModelProviderIcons` uses `res/drawable/ic_provider_*.xml` converted from MIT `@lobehub/icons`
  MONOCHROME SVGs (single `currentColor` path → tintable, never the colored variants, per theme
  discipline). Each drawable carries its upstream-source + license as an XML provenance comment.

Two standing rules: **one icon weight per surface** (the drawer/settings are all `Icons.Filled.*`, so
a new drawer glyph is Filled); and the ephemeral **Temporary project uses `Schedule` (a clock), NOT
an `AutoDelete`/trash glyph** — a delete-affordance glyph next to a selectable, tappable row misreads
as "tap to delete".

## Testing

`ANDROID_HOME=$HOME/android-sdk ./gradlew :app:testPublicDebugUnitTest` — test sources are
flavor-agnostic, but `testDebugUnitTest` does not exist under the distribution flavors, so pick a
flavor. The reducer (`TranscriptReducer`), `SentenceChunker`, and `AssistTurnMachine` suites are the
contract tests; any serialization/event change must keep them green. Tests exercise real code paths
(real JSON fixtures from the live API shape), stubbing only transport. Read
[`test/CLAUDE.md`](app/src/test/java/com/mewbo/aura/CLAUDE.md) for the JVM-test-only house idioms
before writing a new test double.
