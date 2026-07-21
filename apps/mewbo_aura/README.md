# Mewbo Aura — Android Client

Native Android client for [Mewbo](https://github.com/bearlike/Assistant): a Kotlin + Jetpack Compose assistant app that holds the system **assistant role** (orb overlay on the assistant gesture) and ships a full sessions/chat UI over the existing Mewbo session REST + SSE API. The backend owns all orchestration logic — Aura is a thin, polished frontend.

What you get:

- **Chat over live sessions** — streaming transcripts (word-fade in-flight text, markdown finalized text), tool-call folding, plan/sub-agent chips, model picker, project + MCP-tool scoping, document/image attachments.
- **Assist overlay** — a text-first launcher bound to the system assistant role; it hands off to the app on the first query and never renders chat itself.
- **Voice pipeline** — speech-to-text and text-to-speech behind the `Transcriber` / `Synthesizer` interfaces, with fake implementations auto-selected where the platform engines are unavailable (e.g. containerized dev devices).
- **Device tools** — on-device capabilities the agent can call mid-run (alarms, timers, SMS read/send, battery/time/wake), executed locally and reported back to the session as tool results.
- **Settings** — base URL, API key (encrypted at rest via Android Keystore), default project, display name, reduced motion.

## Layout — a standalone Gradle project inside a Python monorepo

This directory is its **own Gradle root** (`settings.gradle.kts` lives here, not at the repo root). Nothing Android leaks upward: build only with `./gradlew` from this directory, never a system Gradle. The repo-wide Python tooling (ruff/mypy/pytest) ignores this subtree; Kotlin quality gates run through Gradle only.

```
apps/mewbo_aura/
├── app/src/main/java/com/mewbo/aura/
│   ├── data/        # API client, SSE, domain model, repos, device tools, settings
│   ├── di/          # Hilt modules
│   ├── ui/          # Compose: chat, composer, navigation, orb, overlay, theme, …
│   └── voice/       # assistant-role service, turn machine, Transcriber/Synthesizer
├── app/src/debug/   # debug-only preview activities + cleartext network config
├── app/src/test/    # JVM unit tests (reducer + chunker contract suites)
└── tools/redroid/   # containerized Android dev device (docker compose)
```

### Architecture

Dependencies flow strictly down; the backend re-resolves scope from each request, so the client resends `model`/`project`/`mcp_tools` context on every query.

```
ui/ (Compose)  →  viewmodels  →  data/ (repos → sse/api → model)
                       ↘  voice/ (Transcriber · Synthesizer seams)
```

- `data/` has zero Compose/Android-UI imports; `ui/` never touches OkHttp/Retrofit — all I/O goes through repositories.
- All audio/speech code lives in `voice/` behind its two interfaces.
- One chat composable tree, one host (`MainActivity` nav). The overlay is a launcher, not a second chat renderer.
- Transcript rows come only from `TranscriptReducer` (pure, idempotent on event identity — reconnect/backlog-replay safe).

Stack: Kotlin 2.4 (AGP 9 built-in), Compose (BOM 2026.06) + Material 3, Hilt, Retrofit/OkHttp (REST + SSE), kotlinx.serialization, DataStore.

## Requirements

- JDK 17+
- Android SDK with API 37 platform (`compileSdk = 37`; forced by AAR floors — see `CLAUDE.md`)
- Device/emulator on API 33+ (`minSdk = 33`)

`local.properties` (gitignored) carries `sdk.dir` for IDE use. In CI/agent shells `ANDROID_HOME` is not exported — prefix commands as shown below.

## Build → install → verify

```bash
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:assemblePublicDebug
adb -s <serial> install -r app/build/outputs/apk/public/debug/app-public-debug.apk
adb -s <serial> shell am start -n com.mewbo.aura/.MainActivity
adb -s <serial> exec-out screencap -p > /tmp/aura.png   # then look at it
adb -s <serial> logcat -d --pid=$(adb -s <serial> shell pidof com.mewbo.aura) | grep -E "FATAL|AndroidRuntime"
```

Always pass `-s <serial>` to every adb/gradle invocation — multiple dev devices can be connected simultaneously.

### Dev devices

| Tier | Device | Use for | Limits |
|---|---|---|---|
| 1 | redroid container (`tools/redroid/`, `adb connect localhost:5555`) | UI, chat, streaming, reducer work | AOSP image: no `SpeechRecognizer`/TTS engine (fake voice pipeline auto-selected); software GPU (judge geometry, never FPS); assistant role/gesture not representative |
| 2 | physical device over Wi-Fi adb | assistant role, gesture overlay, real STT/TTS, haptics | needs a human in the loop |

See `tools/redroid/docker-compose.yml` for container ops (binder module bootstrap, pinned subnet, data wipe) and `CLAUDE.md` for the hard-won details.

## Backend connectivity

The app talks to the Mewbo API (`X-API-Key` auth; SSE uses `?api_key=`). From the redroid container the dev base URL is `http://api:5125` (compose service name — cleartext is allowed by the **debug** network security config only). Configure via Settings, or seed deterministically on debug builds:

```bash
adb -s <serial> shell am start -n com.mewbo.aura/.MainActivity \
  -e seedBaseUrl http://api:5125 \
  -e seedApiKey "<api-key>"
```

Never type keys through the Settings UI in automation — `adb input text` mangles long secrets. A recurring 401 on a dev device almost always means the stored key is gone (container recreated / fresh install), not an auth bug: re-seed.

## Debug-variant tooling (`app/src/debug`)

Launch directly with `adb -s <serial> shell am start -n com.mewbo.aura/<activity>`:

- `.ui.aurora.LivenessShowcaseActivity` — 5-page visual gallery (aurora wash/glow/edge, spark, orb states).
- `ChatPreviewActivity` — chat vocabulary on fake state, no network.
- `AssistOverlayPreviewActivity` — fast-loop overlay verification without the assistant role/gesture.

## Testing & lint

```bash
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:testPublicDebugUnitTest   # JVM unit tests
ANDROID_HOME=$HOME/android-sdk ./gradlew :app:lintDebug           # Android lint
```

The `TranscriptReducer` and `SentenceChunker` suites are the wire-contract tests — any serialization/event change must keep them green. Tests exercise real code paths with real JSON fixtures from the live API shape, stubbing only transport.

## Release builds

`./gradlew :app:assemblePublicRelease` always yields an installable APK: a configured keystore (`AURA_KEYSTORE_B64`, `AURA_KEYSTORE_PASSWORD`, `AURA_KEY_ALIAS`, optional `AURA_KEY_PASSWORD`) wins; otherwise signing falls back to the auto-generated debug keystore. (`public` is the default distribution flavor; the umbrella `assembleRelease` builds every flavor.)

## Further reading

Deeper, agent-oriented guidance lives in the `CLAUDE.md` tree: [`CLAUDE.md`](CLAUDE.md) (build facts, device matrix, workflow rules) with children for [`data/`](app/src/main/java/com/mewbo/aura/data/CLAUDE.md) (wire contract), [`ui/`](app/src/main/java/com/mewbo/aura/ui/CLAUDE.md) (theme/orb/chat rendering), and [`voice/`](app/src/main/java/com/mewbo/aura/voice/CLAUDE.md) (overlay contract). Product specs and deviations are recorded there.

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
