> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../CLAUDE.md) · [root](../../../../../../../../../CLAUDE.md)

# Aura Debug-Variant Tooling — app/src/debug/

Scope: `app/src/debug/java/com/mewbo/aura/` — everything that ships in `debug` builds only, plus the
DI/const bindings that swap between debug and release. R8 strips these from `release`; the `src/release`
mirror carries the permanently-off / no-op counterparts so `main`/`ui` code stays variant-agnostic.

## Inventory

- **Preview hosts** (Hilt-driven, no network / no assistant role):
  - `ui/aurora/LivenessShowcaseActivity` — the design-v2 visual gallery (aurora wash/glow/edge, spark,
    orb states); supersedes the deleted `OrbShowcaseActivity`. The place a shader/state change is judged
    (`ui/aurora/CLAUDE.md` — redroid SwiftShader for geometry, never FPS; verify oscillating uniforms
    across TIME, not just t=0).
  - `ui/chat/ChatPreviewActivity` — chat vocabulary on fake state; also the Compose-stability
    recomposition-counter host (`ui/CLAUDE.md`).
  - `ui/overlay/AssistOverlayPreviewActivity` — the fast-loop overlay host (no assistant-role/gesture).
    **Parity rule:** it must MIRROR the real overlay contract, or it lies. It mirrors v5 auto-listen via
    an `autoListen` intent extra and threads `SettingsStore.reducedMotion` directly (it's
    `@AndroidEntryPoint`) — the SAME bare-`AuraTheme` gap that bit `AuraSession` for a release cycle
    (`ui/CLAUDE.md` § Accessibility). The keep-screen-on flag (`voice/CLAUDE.md`) has a parity one-liner
    here too.
- **Fake voice pipeline** (`voice/FakeTranscriber`, `voice/FakeSynthesizer`, `voice/VoiceBackends`) —
  load-bearing, not test sugar: redroid is AOSP (no `SpeechRecognizer`, no TTS engine). `di/VoiceModule`
  binds the runtime-switchable fakes in debug; release binds the platform impls (`voice/CLAUDE.md`).
- **Mock backend** (`di/MockBackendModule` + `mock/`) — the scripted OkHttp interceptor + scenarios; see
  [`mock/CLAUDE.md`](../../../../../main/java/com/mewbo/aura/mock/CLAUDE.md) (the `main/` seam doc covers both
  halves).
- **`ui/navigation/DebugFlags.kt`** — `const val IS_DEBUG_BUILD = true` (release mirror = `false`), gating
  the two gallery routes in `AuraNavHost` (stands in for the disabled `BuildConfig.DEBUG`).

**Charter — debug-tooling learnings accrue here.** A new debug tool goes in this source set and is
registered in the debug `AndroidManifest.xml`; if it swaps behavior vs release, it needs a `src/release`
mirror so `main` never branches on the variant.
