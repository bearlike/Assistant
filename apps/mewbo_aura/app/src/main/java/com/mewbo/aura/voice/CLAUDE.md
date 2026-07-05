> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Voice — Speech Pipeline Guidance

Scope: `voice/` — STT/TTS seams, platform impls, fakes, VoiceInteraction
services, the `AssistTurnMachine` state machine. Spec: Gitea #175 (v1), #181 (v5 — current). **All
audio/speech code lives here; nothing outside `voice/` touches microphone, recognizer, or TTS
APIs.**

## v5 overlay contract (#181 — supersedes #178's text-first, handoff-only contract)

The assistant-trigger overlay is **voice-first**: it auto-listens on trigger, streams the FIRST
turn's response IN the overlay as a card, and hands off to the app from the SECOND interaction
onward:

- `AuraSession.onShow()` fires the t=0 haptic, calls `AssistTurnMachine.show()` (rests in `Ready`,
  the §7.0 t=0..340ms scrim/edge-sweep/bar choreography itself unchanged and binding), THEN
  immediately calls `AssistTurnMachine.startListening()` if `RECORD_AUDIO` is granted (the same
  `DevicePermissionChecker` seam, #179, the mic tap already used) — a denied grant on this AUTO path
  stays silent (plain `Ready`, no error notice); the quiet `AssistUiState.Error` notice is reserved
  for an EXPLICIT mic tap the user can see fail. `showFlags` (AOSP `SHOW_SOURCE_ASSIST_GESTURE` et
  al.) was investigated as a finer-grained trigger-source discriminator but every source wants the
  same auto-listen behavior here, so it stays unread — nothing in-app ever calls
  `VoiceInteractionService.showSession()`, so on a real device `onShow()` is always externally
  triggered anyway.
- Auto-send on end of speech was ALREADY wired pre-#181 and is unchanged: `TranscriberEvent.Final` →
  `haptics.transcriptAccepted()` → `beginTurn(text, Voice)`.
- `beginTurn`'s FIRST turn (`turnCount == 0`) → `Sending` → `createSession` (model/project from
  `SettingsStore` defaults) → `sendQuery` → `turnCount = 1` → `subscribeLive(sessionId)`, which folds
  `liveEvents(sessionId)` (`RunRepository.live`) through the machine's OWN `TranscriptReducer.State`
  — the pre-v4 machine's exact pattern, recovered from `git show f7e505a:.../AssistTurnMachine.kt`
  for this spec, never a fork of that reducer. State = `AssistUiState.Streaming(items, done, speaking)`;
  a `completion` event (or the upstream flow ending on its own) flips `done = true` +
  `haptics.settle()`. NO handoff fires on the first turn.
- `beginTurn`'s turn TWO ONWARD (`turnCount >= 1`) — from `sendText` or a fresh `startListening`
  while the card is showing — takes the v4 dispatch-then-handoff path unchanged: `sendQuery` on the
  ALREADY-CREATED session (never re-created — `sessionId ?: createSession()`, retry-safe against a
  partial first-turn failure too) → `haptics.settle()` → `onHandoff(sessionId, modality)` →
  MainActivity (`singleTask`, `EXTRA_HANDOFF_SESSION_ID`, handled in BOTH `onCreate` and
  `onNewIntent`) navigates to the chat, which picks up the already-streaming/finished run.
- `AssistTurnMachine.expand()` hands off on the CURRENT session at ANY point (during or after
  streaming) — `ChatViewModel.bind` already attaches to an already-live run either way. A no-op
  before the first turn's session id has resolved.
- `AssistTurnMachine.stopStreaming()` is client-side detach only (v1 semantics, backend run keeps
  going) — cancels the live-collection job, barges in on speech, sets
  `Streaming(items, done = true, speaking = false)`.
- Create/send failure on the FIRST turn = `AssistUiState.Error` (retryText = the failed text, no
  handoff); a lost LIVE connection mid-stream also surfaces as `Error` (retryText = "", nothing
  queued to resend — the send itself already succeeded, only the stream broke).
- `ContinueLastSessionChip` still hands off directly (no query dispatched) regardless of turn count.
- `AssistTurnMachine.pullUpToApp(draft)` (user directive 2026-07-04): the pill pull-up gesture's
  routing seam — sessionId from THIS invocation → same handoff target `expand()` reaches; no
  session → new-chat handoff (`EXTRA_HANDOFF_NEW_CHAT`); a non-blank draft rides along
  (`EXTRA_HANDOFF_DRAFT`) in both cases. Fires the `settle` release haptic. `AuraSession` funnels
  `handleHandoff`/`handlePullUp` through ONE `launchApp(sessionId?, draft?, modality)` — never a
  second launch mechanism. Gesture/UI anatomy: `ui/overlay/CLAUDE.md` § "Pill pull-up → app
  handoff"; unit-tested in `AssistTurnMachineTest` (session/no-session/blank-draft routing).

## Session-reuse lifecycle law (AOSP-verified — the single fact behind several #181 fixes)

The `VoiceInteractionSession` (and therefore this SAME `AssistTurnMachine` instance, constructed
once in `AuraSession.onCreate()`) SURVIVES a hide→show cycle under normal operation — confirmed
against actual AOSP source, not assumed:

- `VoiceInteractionManagerServiceImpl.showSessionLocked()`: a new `VoiceInteractionSessionConnection`
  (and therefore a fresh client-side `onNewSession`) is only constructed `if (mActiveSession ==
  null)`. Every subsequent `showSession()` call while a session is already active routes to
  `mActiveSession.showLocked(...)` on the EXISTING connection instead.
- `VoiceInteractionSessionConnection`: the client `mSession` object is created exactly ONCE, in
  `onServiceConnected()` (→ `AuraSessionService.onNewSession` → `AuraSession(this)`). Every later
  `showLocked()` on an already-connected instance calls `mSession.show(...)` (→ `onShow()`) on that
  SAME session object.
- `mActiveSession` is nulled (permitting a future fresh session) only when the connection tears
  down — `VoiceInteractionSession.finish()` or the service being killed/unbound — NOT on a plain
  `hide()`. `AuraSession` never calls `finish()` anywhere; the only teardown path is `dismissSession()`
  → `machine.dismiss(); hide()`.
- App-side: `onCreateContentView()`'s `ComposeView` uses `ViewCompositionStrategy
  .DisposeOnViewTreeLifecycleDestroyed` — gated on `ON_DESTROY` only. `onHide()` fires `ON_PAUSE`/
  `ON_STOP`, never `ON_DESTROY`. `performRestore(null)` runs exactly once, inside `onCreate()`, which
  itself only fires once per session lifetime per the platform facts above.

**Consequence, binding on any future per-invocation state in this class or its Compose consumers**:
anything that should NOT carry over from one assistant invocation to the next MUST be explicitly
reset in `AssistTurnMachine.dismiss()` (or the Compose-side equivalent, keyed off `AssistUiState.Idle` —
see `ui/overlay/CLAUDE.md`'s draft-clear-on-Idle law for that precedent). `turnCount`/`sessionId`/
`turnModality`/`speechMuted`/`reducerState` are the machine-side precedents — without this reset, a
SECOND invocation would silently inherit the first invocation's turn count and skip straight to
handoff on what the user experiences as their first query of a brand-new overlay. The
mic-invisibility bug (`ui/overlay/CLAUDE.md`) and the stranded-orb bug (`ui/overlay/CLAUDE.md`'s
orb tracking law) are both instances of this SAME underlying fact caught in the UI layer instead.

## `VoiceInteractionSession` window regime (Gitea #181 items 4/6)

Before #181, `onCreateContentView()` never configured the session window's soft-input regime at all
— the only window property either fix ever touched was `isNavigationBarContrastEnforced`. On-device
trace (dumpsys-verified): the platform DEFAULT for this window was `adjust=pan` with a STATIC,
never-resizing full-screen frame — byte-identical across all three IME states. That silently starved
two things at once: the orb's own keyboard-transition jump (nothing was actually resizing to key
off), and part of the documented ~1000px real-session-vs-preview-activity geometry gap (`ui/overlay/CLAUDE.md`'s
own note — re-verify that gap now that both hosts request the same explicit regime). Fix: explicit,
plain `Window` APIs (available since API 30; minSdk here is 33, no `androidx.core` compat helper
needed) set directly in `onCreateContentView()`, right alongside the existing nav-bar-contrast fix:

```kotlin
window?.window?.let { w ->
    w.setDecorFitsSystemWindows(false)
    w.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
}
```

This is the PLATFORM-side half only. It pairs with `.imePadding()` on the Compose side (the overlay
screen's bottom `Column`, `ui/overlay/CLAUDE.md`) — neither alone is sufficient: `imePadding()` on a
window that never actually resizes is inert (same root cause as `ChatSurface`'s own
`adjustResize`-alone finding), and an explicitly resizing window with no `imePadding()` just lets
the IME occlude the composer instead of padding around it.

## The two seams (v1 binding: platform APIs only)

- `Transcriber` — `Flow<TranscriberEvent>`: `Ready · Rms(db) · Partial(text)
  · Final(text) · Error(code)`. Impl: `SpeechRecognizerTranscriber`
  (`android.speech.SpeechRecognizer`, `EXTRA_PARTIAL_RESULTS`,
  `EXTRA_PREFER_OFFLINE`). RMS feeds the orb listening animation directly.
  Consumers: `AssistTurnMachine.startListening()` (overlay, now auto-invoked on `show()` when
  granted — see the v5 contract above) and, since Gitea #180 P2, `ChatViewModel.startDictation()`
  (chat composer's mic) — its own `DictationDecision` pure-maps each event onto
  `ChatUiState.dictation`, 0..1-normalizing the raw dBFS-ish `Rms` reading for `RmsWaveform`.
- `Synthesizer` — `speak(utterance, queueMode)`, `stop()`, `isAvailable`,
  `Flow<SynthEvent>` (Started/Done/Error per utterance). Impl:
  `PlatformSynthesizer` (`android.speech.tts.TextToSpeech`), lazy init,
  graceful degrade: no engine/voice data → `isAvailable=false`, speaker
  toggle disabled, NEVER an error dialog.

These interfaces are the v2 extension point (remote WS STT / neural TTS) —
keep them minimal; don't grow them for a v1 convenience.

## Speak-along (`SpeechController` + `SentenceChunker`) — ACTIVE since v5, `SpeechController` now lives here

`SpeechController` MOVED from `ui/chat/` to `voice/` for Gitea #181 ("all speech in `voice/`," this
file's own binding rule) — its own tests moved with it (`app/src/test/.../voice/SpeechControllerTest.kt`).
`AssistTurnMachine` owns its OWN `SpeechController` instance (`SpeechController(synthesizer, scope)`,
constructed in the machine's own init) — this is a SECOND call site of the same shared class, not a
fork: `com.mewbo.aura.ui.chat.ChatViewModel` (Gitea #180 P3/P4 speak-along + read-aloud) is the
first. The two never contend for the same live turn, because the overlay's in-card first turn
always either hands off or tears down (`dismiss()`) before `ChatViewModel.bind` ever attaches to
that same session.

**Cross-instance handoff law (Gitea #181 fix wave, finding 1):** temporal non-overlap alone doesn't
mean nothing needs reconciling. `ChatViewModel.bind()`'s history replay unconditionally re-folds the
WHOLE persisted transcript through its own reducer, and until this fix that replay ALSO ran through
`ChatViewModel`'s live speak-along pipeline (`publish()` → `speech.onAssistantMessage`) - against a
brand-new `SpeechController` whose `closedKey`/`chunker`/`consumed` all start at zero, with no idea
the OVERLAY's instance already spoke some or all of that same text before the handoff. Deterministic
on a voice-tagged handoff (`activeTurnModality == Voice`), not a race: tapping the response card's
Expand mid-turn re-spoke the in-progress reply from word one on the chat screen (barge-in cut the
overlay's own speech first); a second voice follow-up re-spoke turn 1's already-fully-spoken reply
in full. Fix: `bind()`'s history-replay loop passes `speakAlong = false` (an `applyEvent`/`publish`
parameter, default `true` - byte-equivalent everywhere else), and once the replay settles,
`SpeechController.primeAlreadySpoken(item, modality)` marks whatever text is now present as already
spoken - using the SAME `SentenceChunker.push`-then-discard "consumed cursor" mechanism
`onAssistantMessage` itself relies on - so only genuinely NEW deltas (a still-live run's later
`agent_message_delta`s) reach real speech after that point. Any FUTURE second call site of
`SpeechController` must ask the same question a fresh `bind`/attach onto already-narrated content
does here: does this instance need priming before its first live fold, or could it re-speak
something a sibling instance already said?

`SentenceChunker` is NO LONGER dormant (superseding the pre-#181 "DORMANT since v4" note) — it's
`SpeechController`'s markdown-stripping/sentence-chunking engine, feeding BOTH call sites. Its
invariants (unchanged, still binding):

- Stable `utteranceId = {messageId}:{sentenceIndex}`; queue with `QUEUE_ADD`.
- On `agent_message` reconciliation: already-spoken text is NEVER re-spoken;
  only the unspoken remainder is corrected.
- Feeds plain text: code fences dropped (one "code block omitted"), links →
  link text, emphasis markers stripped. Tool chips are never spoken.
- Barge-in (new LISTENING turn / Stop / dismiss / `toggleSpeak` mute) → `SpeechController.bargeIn()`
  → `Synthesizer.stop()` flushes the queue AND latches the barged-in message's key as permanently
  closed for the rest of that turn (`closedKey`) — idempotent, ≤ 200ms. Unmuting via `toggleSpeak()`
  does NOT retroactively resume a barged-in message mid-sentence; it only re-arms speech for
  whatever assistant text folds in AFTER the unmute.
- Speech never blocks UI state transitions — `Streaming(done = true, ...)` is reachable while audio
  still plays; the STATE's own `speaking` field is forced `false` the instant `done` flips true
  regardless of the synthesizer's actual trailing-utterance tail (the machine doesn't chase it).
- Voice-modality gating lives in `SpeechController.onAssistantMessage`'s own `modality !=
  InputModality.Voice` guard — a Text-modality turn never touches the chunker/synthesizer at all,
  matching "text turns stay completely silent" (Gitea #180 P3).

## Fakes are load-bearing, not test sugar

redroid (primary dev device) is AOSP: `SpeechRecognizer` **does not exist**
there and no TTS engine ships. `FakeTranscriber` (scripted
Ready→Rms→Partial→Final on a timer) and `FakeSynthesizer` (logged
utterances, timed Started/Done) live in the **debug variant** and are what
the entire orb state machine runs on during M1–M3 and in UI tests. DI binds
per build variant: release → platform impls; debug → runtime-switchable
(debug settings toggle). Anything beyond the fakes (real recognizer/TTS,
VoiceInteraction gesture flow) requires the physical Pixel + a human.
`AssistOverlayPreviewActivity` (debug-only fast-loop host) mirrors the v5 auto-listen contract via
an `autoListen` intent extra (default `true`, since `FakeTranscriber` never needs a real permission
grant to begin with) — `-e autoListen false` opts into the non-auto-listen `Ready` path for manual
verification without deleting/re-adding code.

## Audio focus & etiquette

`AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK` for speech duration, abandon on
stop/done. Respect STREAM_MUSIC volume. Mic active ONLY in LISTENING (no
background capture, no foreground service in v1).

## Haptics (`AuraHaptics`, §6.13)

`AuraHaptics` interface + `NoOpAuraHaptics` (default, keeps
`AssistTurnMachine` JVM-testable) + `VibratorAuraHaptics` (real device,
predefined-effect fallback) replaced the old per-state-kind tick. Four named
moments, each wired at one exact call site: `invocation()`
(`AuraSession.onShow()`), `transcriptAccepted()` (voice-Final branch only),
`settle()` (successful handoff dispatch in `beginTurn`'s turn-two-onward branch, AND the first
turn's stream completing — since v5, "settle" no longer means only "handed off"), `error()`
(the `AssistUiState.Error` construction sites). `di/HapticsModule.kt`
resolves the `Vibrator`.

**`android.permission.VIBRATE` is required in the manifest** — without it,
`Vibrator.vibrate()` silently no-ops (no exception, no log): every haptic
in the app, including v1's, was dead for a full release cycle because
nothing checked for it. `:app:lintDebug` catches a missing declaration;
this is why lint runs at every wave boundary now (see root CLAUDE.md).

## Verifying the overlay WITHOUT a Pixel (works on redroid)

Two paths, both proven — try the fast loop FIRST:
- **Fast loop**: debug-only `AssistOverlayPreviewActivity` hosts the real
  `AssistTurnMachine` + `AssistOverlayScreen` via Hilt with no role/gesture
  required. Fast, deterministic — the right first stop for any overlay
  change.
- **Real session**: `adb shell cmd role add-role-holder --user 0
  android.app.role.ASSISTANT com.mewbo.aura` then `adb shell input
  keyevent 219` — invokes the actual VoiceInteractionSession on AOSP;
  FakeTranscriber drives a full scripted voice turn. (The spec assumed
  this was Pixel-only; it isn't.) **Needs a quiet box**: under concurrent
  load from other builds on the same redroid container, this path has shown
  a stray unrelated activity + an OOM-kill instead of the real overlay —
  not a code bug, confirmed by a clean re-run once nothing else was
  building. `am force-stop` clears the ASSISTANT role holder — re-run
  `add-role-holder` after every force-stop.
Real mic/TTS/haptics/gesture-over-apps still need the physical Pixel.
The real session window's on-screen geometry differs from the debug
preview activity's (the composer sits ~1000px lower in device coords) —
when driving it via `adb input tap`, measure coordinates from an actual
screenshot of the session, never reuse preview-activity coordinates. Re-verify this specific gap
post-#181 (`ui/overlay/CLAUDE.md`'s real-session-vs-preview geometry note) — both hosts now request
the same explicit soft-input regime, which may have closed some or all of it.

`VoiceInteractionSession.getWindow()` returns `android.app.Dialog`, not
`android.view.Window` — Kotlin's `window` property resolves to that
`Dialog`, which has no `isNavigationBarContrastEnforced` of its own. Reach
the session's real `Window` via `window.window` (`Dialog` wraps one via its
own `getWindow()`) — needed to fix the same 3-button-nav contrast-scrim
occlusion the debug preview activities hit (it paints an opaque band over
`AuroraEdgeGlow`'s bottom bloom), which recurs on the real session window
too. The `decorFitsSystemWindows`/`setSoftInputMode` calls above go through
this SAME `window.window` reference, right alongside it.

## Timer + init traps (learned live)

- `onRmsChanged` fires CONTINUOUSLY on real recognizers — never reset a
  silence timer on `Rms` events, only on actual speech evidence
  (`Partial` changes).
- `PlatformSynthesizer` buffers utterances issued before async TTS init
  resolves (ordered flush on success, per-id `Error` on failure) — a fast
  SentenceChunker outruns engine init.

## VoiceInteraction services (Pixel-only for real speech)

`AuraVoiceInteractionService` / `AuraSessionService` / `AuraSession`
(ComposeView in `onCreateContentView`) implement the assistant-role overlay
(`RoleManager.ROLE_ASSISTANT`). The session window is a system surface —
requires the `android.voice_interaction` meta-data XML and
`BIND_VOICE_INTERACTION` permission on both services. Compose inside a
`VoiceInteractionSession` needs explicit lifecycle owners on the content
view (`setViewTreeLifecycleOwner` et al.) — the session is not an Activity.
