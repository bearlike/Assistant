> ↑ [root /CLAUDE.md](../../CLAUDE.md) · children: [data](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) · [ui](app/src/main/java/com/mewbo/aura/ui/CLAUDE.md) · [voice](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md) · [test](app/src/test/java/com/mewbo/aura/CLAUDE.md) · [redroid](tools/redroid/CLAUDE.md)

# Mewbo Aura — Android Assistant Client

Scope: `apps/mewbo_aura/` — native Kotlin + Jetpack Compose assistant app
(orb overlay via the system assistant role + sessions/chat UI) over the
existing Mewbo session REST+SSE API. The backend owns all logic; Aura is a
thin, polished frontend. **Spec lineage: #175 (v1) → #176 (design v2) →
#177 (v3: model picker, composer options + attachments, tool-call fold) →
#178 (v4: default project + scope indicator + multi-line composer;
text-first overlay handoff) → #181 (v5: voice-first — auto-listen on
trigger, first turn streams in-overlay as a response card, second turn
onward hands off; aurora shader rebuild)** — deviations are recorded
there, never in ad-hoc docs here.

## CLAUDE.md tree (this app)

The tree grew past 4 files — read the deepest one that applies before editing.

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
| App root: build, device matrix, layering, atomic-class rules | `CLAUDE.md` (this file) |
| Data layer: wire contract, `TranscriptReducer` invariants | `app/src/main/java/com/mewbo/aura/data/CLAUDE.md` |
| UI: theme, orb, chat rendering, screens (thin router) | `app/src/main/java/com/mewbo/aura/ui/CLAUDE.md` |
| Shader family: `AuroraEdgeGlow`/`AuroraWashTop` rebuild rules | `app/src/main/java/com/mewbo/aura/ui/aurora/CLAUDE.md` |
| Assist overlay: response card, orb tracking, draft-clear laws | `app/src/main/java/com/mewbo/aura/ui/overlay/CLAUDE.md` |
| Composer: pill anatomy, dictation contract, style tokens | `app/src/main/java/com/mewbo/aura/ui/composer/CLAUDE.md` |
| Voice: `AssistTurnMachine`, `VoiceInteractionSession` lifecycle, speak-along | `app/src/main/java/com/mewbo/aura/voice/CLAUDE.md` |
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
= `res/raw/enterprise_ca.crt`). Gitea prereleases (`0.0.XX-debug`) ship `enterpriseDebug`,
built locally + attached via `tea`.

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

Debug builds ship: `LivenessShowcaseActivity` (6-page design-v2 visual
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
- One chat composable tree (`ChatTranscript`), reused as a COMPONENT by both hosts — never forked.
  `MainActivity` nav is still the ONE app-navigation host; since v5/#181 the assist overlay ALSO
  renders chat again (a `ResponseCard` for the first turn's streaming response, reusing the exact
  same `ChatTranscript` `ChatSurface` does), then hands off to `MainActivity` from the second
  interaction onward. See `ui/overlay/CLAUDE.md` and `voice/CLAUDE.md` for the full v5 contract —
  superseded v4/#178's "overlay never renders chat" rule.

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

## Testing

`ANDROID_HOME=$HOME/android-sdk ./gradlew :app:testPublicDebugUnitTest` (test
sources are flavor-agnostic; `testDebugUnitTest` no longer exists under the
distribution flavors — pick either flavor) — the
reducer (`TranscriptReducer`), `SentenceChunker`, and `AssistTurnMachine` suites are the contract
tests; any serialization/event change must keep them green. Tests exercise
real code paths (real JSON fixtures from the live API shape), stubbing only
transport. See `app/src/test/java/com/mewbo/aura/CLAUDE.md` for JVM-test-only house idioms
(coroutine scopes, scripted fakes, timestamp parsing) before writing a new test double.
