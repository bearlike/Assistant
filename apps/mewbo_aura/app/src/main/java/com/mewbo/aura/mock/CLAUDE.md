> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Mock Backend — mock/

Scope: `mock/` — the zero-token scripted backend for on-device UI/overlay/gate testing. The `main/`
source set holds ONLY the seam; the interceptor and all scripted content are **debug-only**
(`app/src/debug/.../mock/`). Toggle via Settings → Debug → "Use mock backend", or for automation
`adb ... am start -n com.mewbo.aura/.MainActivity -e mockBackend true`.

## main/ holds only the seam; debug/ holds the machinery

- **`main/mock/MockBackendFlags.kt`** — the debug/release-swapped seam interface (same pattern as
  `voice/`'s `Transcriber`/`Synthesizer`). Lives in `main/` so `ui/settings` + `MainActivity` inject it
  uniformly; release binds a permanently-off no-op. **Deliberately NOT part of `SettingsStore`** —
  `data/` carries no concept of a mock backend. `isEnabledBlocking()` is the sync snapshot the
  interceptor reads (it runs on OkHttp's chain and can't suspend — same shape as `AuthInterceptor`).
- **`debug/di/MockBackendModule.kt`** — `@Binds` the impl + `@Provides @IntoSet` contributes
  `MockBackendInterceptor` into the app-wide `Set<Interceptor>` multibinding
  [`DataModule`](../di/CLAUDE.md) consumes. Release contributes nothing (empty set), so
  `provideOkHttpClient` is variant-invariant.

## The interceptor + the ONE scripted-content file

- **`MockBackendInterceptor`** is an APPLICATION interceptor (synthesizes a `Response` with no socket).
  `flags.isEnabledBlocking()` is checked at the START of every call, so flipping the Settings row takes
  effect on the next request with NO process restart. It covers BOTH Retrofit and SSE (the EventSource
  client is `newBuilder()`-copied, [`di/CLAUDE.md`](../di/CLAUDE.md)). The SSE handler streams scripted
  frames through an `okio.Pipe` on a background `Thread` with REAL per-frame pacing and appends the
  terminal `stream_end` control frame at the transport layer. `"terminate"` in a query → a `410`
  `session_terminated` envelope (the exact shape `RunRepository.errorFor` keys on). **Unhandled paths
  return `501`, never a fake `200`** — an honest gap.
- **`MockScenarios.kt` is the ONE place scripted content lives** (user directive: not sprinkled).
  Frames are hand-built `JsonObject`s matching the REAL wire field-for-field — bare `{type, ts, payload}`,
  snake_case, **NO synthetic `"error"` event type** (failures surface via `completion.error`, exactly
  as [`data/CLAUDE.md`](../data/CLAUDE.md) requires of production). `MockScenariosTest` round-trips
  every frame through the real `SessionEvent.decode` — that round-trip IS the contract-test value (see
  [`test/CLAUDE.md`](../../../../../../test/java/com/mewbo/aura/CLAUDE.md)). Timestamps are re-shaped
  from `Instant.toString()`'s `Z` to the `+00:00` numeric offset (the Android ISO-parse trap).
  Selection is by query keyword: `error`/`long`/`alarm`/`widget`/else `happyPath`. **`alarmScenario` is
  the only token-free path to a promoted-tool action card** (`tool_input` MUST be a JSON object or
  `AlarmArgs.parse` degrades to the generic card); **`widgetScenario` is the only token-free path to
  the stlite WebView card** (`{app.py, data.json}`, both required).
- **`MockSessionStore.seedDemoIfEmpty()`** seeds 6 demo sessions — mixed-origin (4 `mobile` + 2 `user`)
  and date-spread across all three `SessionGrouping` buckets — so the grouped rail + the mobile-only
  default filter are testable with no live backend. No-op once any real session exists. Note the two
  caps differ: create-title `TITLE_MAX_CHARS = 80` here vs the `120`-char RENAME cap.

**Charter — session learnings about the mock accrue here.** A new scenario is one entry in
`MockScenarios` + its `SessionEvent.decode` round-trip in `MockScenariosTest`, and nothing else.
