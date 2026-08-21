> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../CLAUDE.md) · [root](../../../../../../../../../CLAUDE.md)

# Aura Debug-Variant Tooling — app/src/debug/

Scope: `app/src/debug/java/com/mewbo/aura/` — everything that ships in `debug` builds only, plus the
DI/const bindings that swap between debug and release. R8 strips these from `release`; the `src/release`
mirror carries the permanently-off / no-op counterparts so `main()`/`ui` code stays variant-agnostic.

## Inventory

- **Preview hosts** (Hilt-driven, no network / no assistant role):
  - `ui/aurora/LivenessShowcaseActivity` — the visual gallery (aurora wash/glow/edge, spark, orb
  states) and the place a shader/state change is judged (`ui/aurora/CLAUDE.md` — redroid SwiftShader
  for geometry, never FPS; verify oscillating uniforms across TIME, not just t=0).
  - `ui/chat/ChatPreviewActivity` — chat vocabulary on fake state; also the Compose-stability
  recomposition-counter host (`ui/CLAUDE.md`).
  - `ui/overlay/AssistOverlayPreviewActivity` — the fast-loop overlay host (no assistant-role/gesture).
  **Parity rule:** it must MIRROR the real overlay contract, or it lies. It mirrors v5 auto-listen via
  an `autoListen` intent extra and threads `SettingsStore.reducedMotion` directly (it's
  `@AndroidEntryPoint`) — the SAME bare-`AuraTheme` gap that bit `AuraSession` for a release cycle
  (`ui/CLAUDE.md` § Accessibility). The keep-screen-on flag (`voice/CLAUDE.md`) has a parity one-liner
  here too.
  - `ui/speech/SpeechBenchActivity` — the TTS bench: type text, pick any gateway engine or the
  on-device one, speak, and read the latency, the audio byte count and the transport's own failure
  reason. **It substitutes exactly ONE thing — the `SpeechEngineGate`** — so `SelectedSynthesizer`
  and `RemoteSynthesizer` run verbatim; a bench that re-implemented synthesis would measure itself.
  Two traps it encodes: **the gate has TWO readers** (the router picks a delegate, then
  `RemoteSynthesizer.pump` re-reads it for the model id on the wire), so swapping only the router's
  gate reports one engine's name over another's latency; and `SynthEvent.Error` carries an id and no
  reason, which is why `RecordingSpeechGateway` records the call as it passes rather than a
  production event being widened. The on-device leg is the injected `@OnDeviceSpeech` SINGLETON (it
  owns a TTS engine + an audio-focus request — a second copy would contend with read-aloud), while
  the remote leg is bench-local because the `@Singleton` one is welded to the real gate.
- **Fake voice pipeline** (`voice/FakeTranscriber`, `voice/FakeSynthesizer`, `voice/VoiceBackends`) —
  load-bearing, not test sugar: redroid is AOSP (no `SpeechRecognizer`, no TTS engine). `di/VoiceModule`
  binds the runtime-switchable fakes in debug; release binds the platform impls (`voice/CLAUDE.md`).
- **Mock backend** (`di/MockBackendModule` + `mock/`) — the scripted OkHttp interceptor + scenarios; see
  [`mock/CLAUDE.md`](../../../../../main/java/com/mewbo/aura/mock/CLAUDE.md) (the `main/` seam doc covers both
  halves).
- **`DebugFlags.kt`** — `const val IS_DEBUG_BUILD = true` (release mirror = `false`), gating
  the two gallery routes in `AuraNavHost` (stands in for the disabled `BuildConfig.DEBUG`). It sits at
  the app ROOT package, not under `ui/`: `data/device/shizuku` reads it too, and a `data/`→`ui/` import
  is exactly what [`di/CLAUDE.md`](../../../../../main/java/com/mewbo/aura/di/CLAUDE.md)'s seam law
  forbids.

## A capability document can advertise ZERO models and still serve

Measured on the deployed gateway: `GET /api/speech/capabilities` answers `available: true` with
`models: []`, while `POST /api/speech/synthesize` against the id in `defaults.model` returns a
325,676-byte WAV in ~1.0s. Model DISCOVERY is refused for the runtime credential; synthesis is not.

**So an empty list is not "the gateway offers nothing", and a picker built only from the catalogue
cannot reach a working gateway at all.** `SpeechBenchScreen` therefore carries a hand-typed model-id
row and says so in its header. Any surface that enumerates speech engines needs an answer to this
state — reporting "0 engines" there blames the wrong component.

**Charter — debug-tooling learnings accrue here.** A new debug tool goes in this source set and is
registered in the debug `AndroidManifest.xml`; if it swaps behavior vs release, it needs a `src/release`
mirror so `main()` never branches on the variant.
