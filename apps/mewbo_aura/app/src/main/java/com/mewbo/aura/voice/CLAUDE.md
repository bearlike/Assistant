> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../../CLAUDE.md) · [root](../../../../../../../../../../CLAUDE.md)

# Aura Voice — Speech Pipeline Guidance

Scope: `voice/` — STT/TTS seams, platform impls, fakes, VoiceInteraction services, and the
`AssistTurnMachine` state machine. **All audio/speech code lives here; nothing outside `voice/`
touches microphone, recognizer, or TTS APIs.**

## Overlay turn contract

The assistant-trigger overlay is **voice-first**: it auto-listens on trigger, streams the FIRST turn's
response IN the overlay as a card, and hands off to the app from the SECOND interaction onward.

- `AuraSession.onShow()` fires the t=0 haptic, calls `AssistTurnMachine.show()` (rests in `Ready`; the
  t=0..340ms scrim/edge-sweep/bar choreography is unchanged and binding), THEN immediately calls
  `startListening()` if `RECORD_AUDIO` is granted (the same `DevicePermissionChecker` seam the mic tap
  uses). **A denied grant on this AUTO path stays silent** (plain `Ready`, no error notice); the quiet
  `AssistUiState.Error` notice is reserved for an EXPLICIT mic tap the user can see fail. `showFlags`
  (`SHOW_SOURCE_ASSIST_GESTURE` et al.) stays unread — every trigger source wants the same auto-listen
  behavior, and nothing in-app calls `VoiceInteractionService.showSession()`, so on a real device
  `onShow()` is always externally triggered anyway.
- Auto-send on end of speech: `TranscriberEvent.Final` → `haptics.transcriptAccepted()` →
  `beginTurn(text, Voice)`.
- **First turn** (`turnCount == 0`) → `Sending` → `createSession` (project from
  `SettingsStore.selectedProject`; model from `SettingsStore.overlayDefaultModel` — the OVERLAY's own
  default, independent of the app's `selectedModel`; both `createSession` and any turn-two `sendQuery`
  read the SAME overlay default so `context.model` is consistent, and on handoff `ChatViewModel.bind()`
  hydrates that model from the session's persisted context) → `sendQuery` → `turnCount = 1` →
  `subscribeLive(sessionId)`, which folds `liveEvents(sessionId)` (`RunRepository.live()`) through the
  machine's OWN `TranscriptReducer.State` — never a fork of that reducer. State =
  `AssistUiState.Streaming(items, done, speaking)`. **NO handoff fires on the first turn.**
- **THREE events are terminal for `Streaming`: `completion`, `stream_end` AND `stream_error`.** The
  first two arrive on the wire, always in that order, matching `TranscriptReducer`'s own data-layer
  contract. Treating only `Completion` as terminal let `stream_end` fall into the "still streaming"
  branch and reset `done` back to `false` right after `completion` had set it `true`, wedging the
  overlay composer in `Streaming` forever.
  **`stream_error` wedged it the same way, for a subtler reason, and this doc previously stated the
  law in a way that hid it.** `RunRepository.live()` MATERIALIZES an upstream failure into a
  `StreamError` VALUE (`.catch { emit(...) }`) so every follower can see it — so it never THROWS, and
  the machine's own `catch (e: Exception)` cannot fire for it; the SharedFlow never completes, so the
  post-collect fallback cannot either; and `TranscriptReducer` DROPS the event (its comment says
  "intercepted by ChatViewModel", which is true for chat and false here). Three independent safety
  nets, none of which catch it. It routes to `AssistUiState.Error` via `failStreaming`, not to
  `finishStreaming` — a lost connection is not a turn that finished, and the settle haptic would say
  it was. **The stopping set is written in three surfaces that legitimately want different ones**
  (`voice/`, `notify/RunNotificationController`, `ui/chat/ChatViewModel`); the other two are named
  predicates and this one is an inline `val terminal =`, which is how it got missed. Adding a fourth
  transport-terminal arm to `SessionEvent` means checking all three.
  `RunRepository.live()`'s `shareIn(… WhileSubscribed …)` SharedFlow never itself completes, so there is
  no "upstream ended on its own" fallback — every natural completion hits this path. Either terminal
  event flips `done = true` + `haptics.settle()`; an idempotency guard (fire only when not already
  `done`) keeps the pair from double-firing `finishStreaming()`/the haptic.
- **Turn two onward** (`turnCount >= 1`, from `sendText` or a fresh `startListening()` while the card is
  showing): `sendQuery` on the ALREADY-CREATED session (never re-created — `sessionId ?:
  createSession`, retry-safe against a partial first-turn failure) → `haptics.settle()` →
  `onHandoff(sessionId, modality)` → `MainActivity` (`singleTask`, `EXTRA_HANDOFF_SESSION_ID`, handled
  in BOTH `onCreate()` and `onNewIntent`) navigates to the chat, which picks up the already-streaming run.
- `expand()` hands off on the CURRENT session at ANY point (during or after streaming) —
  `ChatViewModel.bind()` attaches to an already-live run either way. A no-op before the first turn's
  session id has resolved.
- `stopStreaming` is client-side detach only (the backend run keeps going) — cancels the
  live-collection job, barges in on speech, sets `Streaming(items, done = true, speaking = false)`.
- Create/send failure on the FIRST turn = `AssistUiState.Error` (retryText = the failed text, no
  handoff); a lost LIVE connection mid-stream also surfaces as `Error` (retryText = "", nothing queued
  to resend — the send succeeded, only the stream broke).
- `ContinueLastSessionChip` hands off directly (no query dispatched) regardless of turn count.
- `pullUpToApp(draft)` is the pill pull-up gesture's routing seam: sessionId from THIS invocation →
  the same handoff target `expand()` reaches; no session → new-chat handoff
  (`EXTRA_HANDOFF_NEW_CHAT`); a non-blank draft rides along (`EXTRA_HANDOFF_DRAFT`) in both cases.
  Fires the `settle()` release haptic. `AuraSession` funnels `handleHandoff`/`handlePullUp` through ONE
  `launchApp(sessionId?, draft?, modality)` — never a second launch mechanism. Gesture/UI anatomy:
  [`ui/overlay/CLAUDE.md`](../ui/overlay/CLAUDE.md); unit-tested in `AssistTurnMachineTest`.

## Session-reuse lifecycle law (AOSP-verified — the fact behind several fixes)

The `VoiceInteractionSession` — and therefore this SAME `AssistTurnMachine` instance, constructed once
in `AuraSession.onCreate()` — SURVIVES a hide→show cycle. Confirmed against AOSP source, not assumed:

- `VoiceInteractionManagerServiceImpl.showSessionLocked()`: a new `VoiceInteractionSessionConnection`
  (and therefore a fresh client-side `onNewSession`) is only constructed `if (mActiveSession == null)`.
  Every subsequent `showSession()` while a session is active routes to `mActiveSession.showLocked(...)`
  on the EXISTING connection.
- `VoiceInteractionSessionConnection`: the client `mSession` object is created exactly ONCE, in
  `onServiceConnected()` (→ `AuraSessionService.onNewSession` → `AuraSession(this)`). Every later
  `showLocked()` calls `mSession.show(...)` (→ `onShow()`) on that SAME object.
- `mActiveSession` is nulled only when the connection tears down — `VoiceInteractionSession.finish()`
  or the service being killed/unbound — NOT on a plain `hide()`. `AuraSession` never calls `finish()`;
  its only teardown path is `dismissSession()` → `machine.dismiss; hide`.
- App-side: `onCreateContentView()`'s `ComposeView` uses
  `ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed` — gated on `ON_DESTROY` only. `onHide()`
  fires `ON_PAUSE`/`ON_STOP`, never `ON_DESTROY`. `performRestore(null)` runs exactly once, inside
  `onCreate()`.

**Consequence, binding on any future per-invocation state in this class or its Compose consumers:**
anything that should NOT carry over from one assistant invocation to the next MUST be explicitly reset
in `AssistTurnMachine.dismiss()`, or the Compose-side equivalent keyed off `AssistUiState.Idle` (see
[`ui/overlay/CLAUDE.md`](../ui/overlay/CLAUDE.md)'s draft-clear-on-Idle law).
`turnCount`/`sessionId`/`turnModality`/`speechMuted`/`reducerState` are the machine-side precedents —
without the reset, a SECOND invocation silently inherits the first's turn count and skips straight to
handoff on what the user experiences as their first query.

## Device tools from the overlay (`AssistOverlayPresence`)

The overlay follows a real run, so it gets `device_*` tools whether or not anyone thought about it —
and both halves of that have been wrong at once.

- **Servicing is NOT wired here, and must never be.** `AssistTurnMachine` subscribes to
  `liveEvents(sessionId)` (`RunRepository.live()`) for transcript rendering, and `live()` builds
  `DeviceToolDispatch` INTO the flow it returns — so the overlay answers `device_tool_call`s for free;
  merely following the run is enough. **Do NOT subscribe an executor from any `voice/` class.** The
  same-seam law and the cost of getting it wrong: [`data/CLAUDE.md`](../data/CLAUDE.md) § "Device
  tools".
- **`AssistOverlayPresence.visible` must be set in `onShow()` and cleared on EVERY teardown path** —
  `onHide()` AND `onDestroy()` (a session killed/unbound without a preceding hide would otherwise leave it
  stuck true forever). Set it BEFORE `machine.startListening()`: the turn's own device tool calls can
  land while this window is still the only thing on screen.

Why the flag exists at all (AOSP `main()` primary source — do not "simplify" it away):

- `VoiceInteractionSessionConnection.showLocked()` rebinds the hosting service with
  `BIND_TREAT_LIKE_VISIBLE_FOREGROUND_SERVICE`, pinning the process at
  `PROCESS_STATE_FOREGROUND_SERVICE`, which `ActivityManager.procStateToImportance()` maps to
  `IMPORTANCE_FOREGROUND_SERVICE` (**125**), never `IMPORTANCE_FOREGROUND` (**100**). Importance counts
  UP as it gets less important, so an `importance <= 100` gate reads a legitimately-showing overlay as
  "backgrounded" and refuses every `device_set_alarm`/`device_set_timer`/`device_dismiss_alarm`.
- The launch IS permitted there: `WindowState` sets `mCallingUidHasNonAppVisibleWindow` for any window
  type `>= FIRST_SYSTEM_WINDOW`, and `TYPE_VOICE_INTERACTION` is `FIRST_SYSTEM_WINDOW + 31`;
  `BackgroundActivityStartController` checks that flag unconditionally and returns
  `BAL_ALLOW_NON_APP_VISIBLE_WINDOW`. (The same `showLocked()` bind also carries
  `BIND_ALLOW_BACKGROUND_ACTIVITY_STARTS`.) Both exemptions live ONLY while the session is SHOWN —
  exactly the window the flag tracks.
- **A BAL-blocked activity start is a SILENT no-op**, which is why a stale-`true` flag is the dangerous
  direction and every ambiguous path errs toward `false`: `ActivityStarter` returns `START_ABORTED`
  internally, but `getExternalResult()` maps it to `START_SUCCESS` before the caller sees it (the only
  trace is a platform-side `Slog.wtf`). A handler would report `handed_to_clock_app: true` for an alarm
  the user never got. Stale-`false` merely surfaces the honest, retryable `app_not_foreground` error.
- **Rejected alternative, do not "improve" it back:** `VoiceInteractionSession.startAssistantActivity()`
  IS the platform's first-class BAL-exempt API for this, but using it would mean plumbing the
  `VoiceInteractionSession` object down into `data/device/` handlers — inverting the app's layering
  (`voice/` → `data/`, never back up) for a case the visible-window exemption already covers — and it
  is API 34+.

## `VoiceInteractionSession` window regime

`onCreateContentView()` sets exactly one window property beyond the nav-bar-contrast fix:

```kotlin
window?.window?.setDecorFitsSystemWindows(false)
```

**There is NO `setSoftInputMode` call anywhere** — the soft-input mode is left at the platform default,
because the WindowManager force-pans `TYPE_VOICE_INTERACTION` windows regardless. Full contract and the
measurements behind it: [`ui/overlay/CLAUDE.md`](../ui/overlay/CLAUDE.md) § "IME: the platform pan is
the ONLY keyboard mechanism".

`VoiceInteractionSession.getWindow()` returns `android.app.Dialog`, not `android.view.Window` —
Kotlin's `window` property resolves to that `Dialog`, which has no `isNavigationBarContrastEnforced` of
its own. Reach the session's real `Window` via `window.window`. Needed to fix the 3-button-nav
contrast-scrim occlusion (it paints an opaque band over `AuroraEdgeGlow`'s bottom bloom), which recurs
on the real session window too.

**A THIRD flag rides this same `window.window` reference — `FLAG_KEEP_SCREEN_ON`:** set in `onShow()`
so the display never dims while the assistant is on screen (a voice turn can run long with no touch
input to reset the dim timer), and cleared on BOTH teardown paths, `onHide()` AND `onDestroy()` —
mirroring the `AssistOverlayPresence.visible` set-in-`onShow()`/clear-on-every-teardown pattern. The
debug `AssistOverlayPreviewActivity` carries a parity one-liner in its own `onCreate()`. DESIGN.md
registers this as the overlay keep-screen-on law.

## The two seams (platform APIs only)

- `Transcriber` — `Flow<TranscriberEvent>`: `Ready · Rms(db) · Partial(text) · Final(text) ·
  Error(code)`. Impl: `SpeechRecognizerTranscriber` (`android.speech.SpeechRecognizer`,
  `EXTRA_PARTIAL_RESULTS`, `EXTRA_PREFER_OFFLINE`). RMS feeds the orb listening animation directly.
  Consumers: `AssistTurnMachine.startListening()` (overlay) and `ChatViewModel.startDictation()` (chat
  composer's mic) — its own `DictationDecision` pure-maps each event onto `ChatUiState.dictation`,
  0..1-normalizing the raw dBFS-ish `Rms` reading for `RmsWaveform`.
- `Synthesizer` — `speak(utterance, queueMode)`, `stop()`, `isAvailable`, `Flow<SynthEvent>`
  (Started/Done/Error per utterance). Impl: `PlatformSynthesizer`
  (`android.speech.tts.TextToSpeech`), lazy init, graceful degrade: no engine/voice data →
  `isAvailable=false`, speaker toggle disabled, NEVER an error dialog.

These interfaces are the extension point for the server-backed engines below — keep them minimal.

## Engine selection — the user picks, the DI seam routes

Each seam now has THREE implementations, and no consumer knows it. `SelectedTranscriber` /
`SelectedSynthesizer` are what Hilt binds to the bare `Transcriber`/`Synthesizer`; they route
between an `@OnDeviceSpeech` leg (the platform impl in release, `VoiceBackends`' fake/platform
switch in debug) and an `@ServerSpeech` one (`RemoteTranscriber`/`RemoteSynthesizer`), on the
user's Settings choice. **The selection lands at the binding, never at a call site** — the overlay
turn, the composer mic, read-aloud and speak-along all honour it by construction, and a fourth
consumer inherits it for free.

- **Blank stored id = on device, and that is the default for both directions**
  (`SpeechCatalog.ON_DEVICE`) — nothing leaves the phone unless someone chose that.
- **An on-device SELECTION is not a promise the device can keep it, and `SelectedTranscriber` now
  checks.** Some televisions — Fire TV among them — ship no `RecognitionService` at all, so the
  platform recognizer never fires. Before this fallback, the debug build's own `VoiceBackends`
  auto-detected that exact unavailability and silently substituted `FakeTranscriber` — correct for
  redroid (no hardware, use the dev double) but wrong for a real unavailable device, where it meant
  returning a scripted, fabricated transcript as if it were real speech. `SelectedTranscriber` now
  asks `SpeechRecognitionAvailability` (a `fun interface` over
  `SpeechRecognizer.isRecognitionAvailable`, bound in `di/SpeechModule`, the same narrow-seam
  treatment as `SpeechEngineGate`) and routes to the `@ServerSpeech` leg when on-device is selected
  but unavailable — a real transcription over a fabricated one. **`SpeechCatalog.ON_DEVICE` is stored
  blank, and blank is DOUBLY overloaded** — "never touched this setting" and "explicitly chose
  on-device" read identically, so this fallback cannot honour "the user explicitly chose on-device,
  never let audio reach a server" any more strictly than "on-device is merely the untouched default".
  Recorded as a gap rather than closed: distinguishing the two readings needs a new stored tri-state,
  out of scope for this fallback. Given the choice, routing to the real server engine is still the
  better of the two outcomes — the alternative is not silence, it is a scripted lie.
- The selection arrives as `SpeechEngineGate`, a `fun interface` over `SettingsStore`'s two flows,
  for the reason `DeviceToolGate` exists: `SettingsStore` reaches the Android Keystore, so
  injecting it would force every routing test onto Robolectric to assert a pure branch.
- Network goes through `SpeechGateway`, declared HERE and implemented by `data/repo/
  SpeechRepository` — so `voice/` never sees a DTO, and the whole `/api/speech` contract has one
  reconcile point.
- **The transcriber re-reads the choice per `listen()`; the synthesizer LATCHES it for a whole
  run.** `speak()` is queue-append and `SpeechController` calls it per sentence, so resolving per
  call would split one reply across two engines whose queues know nothing about each other — they
  would overlap rather than take turns. The latch clears on `stop()`, which is barge-in, which is
  already "this run is over". `stop()` reaches BOTH delegates so a switch mid-playback cannot
  leave one still talking.

## Server-backed STT differs from the platform recognizer, structurally

The transport is request/response — there is no WebSocket anywhere in this app and none was added
— so `RemoteTranscriber` records with `AudioRecord`, endpoints on silence itself, and uploads one
complete utterance. Three consequences, none of them fixable without a streaming transport:

- **No `Partial` events, ever.** The composer shows the amplitude waveform and the text appears at
  the end. `Rms` IS emitted (remapped into `SpeechRecognizer`'s own `-2..10` range, which
  `DictationDecision` and the orb are calibrated against), so the listening animation is unchanged.
- **A manual stop keeps nothing.** `ChatViewModel.stopDictation` salvages the last PARTIAL, and
  there are none. Falling silent is the path that produces a transcript.
- **⚠️ `AssistTurnMachine`'s 8s silence timer resets only on `Ready` and a CHANGED `Partial`** —
  deliberately never on `Rms`. With no partials nothing resets it, so on the OVERLAY an utterance
  running past ~6s of speech is cut off before the endpointer finishes. Chat dictation has no such
  timer. **Unfixed on purpose** (filed, not forgotten): widening that rule edits a contract-tested
  law, and the two candidate fixes both have costs worth choosing deliberately.

**`AudioRecord` + WAV is a decision, not a default.** `MediaRecorder` writing `.m4a` would be ~4x
smaller, and is rejected because it exposes only a polled `getMaxAmplitude()` — this package needs
the raw sample buffers both for the `Rms` events the orb and waveform render and for the silence
endpointer, which is the ONLY thing deciding an utterance has ended. 20s of PCM is ~640 KB against
a 10 MiB server cap. Anyone revisiting this for bandwidth replaces the endpointer first.

## Volume boost — neither playback API can exceed 1.0x, and both of them clamp

A television often has no volume rocker, and a remote's volume keys usually drive the panel or an
AVR rather than Android's media stream — so the assistant can be inaudible at the device's own
maximum. **The two obvious levers are both attenuators, verified in AOSP source rather than assumed:**

- `TextToSpeech.Engine.KEY_PARAM_VOLUME` is *"a float ranging from 0 to 1 where 0 is silence, and 1
  is the maximum volume (the default behavior)"*; the framework carries it as
  `TextToSpeechService.AudioOutputParams.mVolume` (*"[0.0f, 1.0f]"*) to `AudioTrack.setVolume`.
- `MediaPlayer.setVolume` reaches that same gain. `AudioTrack`'s `clampGainOrLevel` hard-clamps
  both at `GAIN_MAX = 1.0f`.

The platform's supported route above unity is `android.media.audiofx.LoudnessEnhancer`, attached to
an audio SESSION, parametrized in millibels. `SpeechVolumeBoost` is the one class that owns it —
state (the gain, the live attachment) plus behaviour (attach, release), with the effect itself
behind an injected `AudioBoostPlatform` and the user's level behind a `SpeechVolumeBoostGate` flow,
the same narrow-seam treatment `SpeechEngineGate` gets and for the same reason: clamping, dB→mB and
the refusal latch are unit-tested with no `AudioManager` anywhere.

- **Both synthesizers consume it and neither owns it.** `PlatformSynthesizer` puts the returned id
  in `TextToSpeech.Engine.KEY_PARAM_SESSION_ID` (the params Bundle used to be `null`);
  `RemoteSynthesizer` calls `MediaPlayer.setAudioSessionId` BEFORE `setDataSource`, so one effect
  spans every clip of a reply. Both call `release()` from their own `stop()` — the barge-in
  boundary this package already treats as end-of-run.
- **`null` means "carry on exactly as before".** Off attaches nothing (not an effect at zero gain),
  and a refused attach returns `null` too, so the untouched playback path is always the fallback and
  there is no half-attached state.
- **A refusal is latched per LEVEL.** `PlatformSynthesizer` asks once per SENTENCE, so without the
  latch a device with no effect library would re-instantiate and re-fail `LoudnessEnhancer` for
  every sentence of every reply. Changing the level earns a fresh attempt.
- **⚠️ A successful attach is NOT a promise the boost is audible on the on-device leg, and nothing
  can tell.** AOSP's `BlockingAudioTrack` builds its `AudioTrack` with the session id, so an engine
  returning audio through `SynthesisCallback` is boosted — but an engine that plays its own audio
  out of band never sees the bundle. That is why `SpeechBoostState.Applied` renders as NO status
  claim in Settings and only a measured `Refused` surfaces; "Supported" here would be a guess.
- `MODIFY_AUDIO_SETTINGS` is declared in the manifest, and the comment there states what it does and
  does not gate — AOSP requires it for an effect only on the global output mix
  (`AudioFlinger::createEffect`), which we never attach to. This is not a second VIBRATE case.
- **Unverified, and it cannot be verified here:** redroid ships no TTS engine and nothing has been
  heard on hardware. Every claim above is source-level or unit-level.

## Failure handling — a gap is worse than an ending

**`TranscriberError.ServiceFailed` is the one STT error that is NOT a quiet-cancel.** Every other
code describes something the user can see for themselves; this one means their recording was
captured and thrown away for an invisible reason, with the remedy being a setting they chose. The
overlay raises `AssistUiState.Error`, the composer raises a notice naming Settings, and the mic
stays enabled — the SERVICE failed, not the device. Widening the loud branch to any other code
would put a card in front of someone who simply said nothing.

**On the TTS side, a failed sentence ENDS the read; it is never skipped.** `SpeechQueueOutcome`
owns that rule (extracted so it is testable at all — `RemoteSynthesizer` needs `Context`,
`AudioManager` and `MediaPlayer`), and it deliberately has no "carry on" member. Skipping makes a
listener hear a sentence vanish from the middle of a reply with nothing to indicate it happened,
which is worse than the audio simply stopping. Exactly one failure is retried, once: the server's
own `503 speech_capacity_exhausted`, which states both that it is transient and how long to wait.
Every dropped utterance is still answered with a `SynthEvent.Error`, because `SpeechController`
clears its speaking state only on an event for its own `lastEnqueuedId` — a silent drain would
strand the read-aloud glyph lit forever at BOTH its render sites, the chat action row's trailing
speaker and the assist overlay's `ResponseCard` speaker badge (`ChatIcons.VolumeUp`, shared).

## Aura strips markdown on the client, and the console no longer does

The console posts raw markdown and lets the server verbalize it — a table gains a spoken
header announcement, an ordered list keeps its ordinals. **Aura cannot follow, and the
divergence is a constraint rather than drift left to tidy up.**

One `SentenceChunker` feeds one `Utterance` stream into whichever `Synthesizer` the user
selected, and `PlatformSynthesizer` — the ON-DEVICE default — hands that text straight to
`TextToSpeech.speak`. There is no server in that path to do the verbalizing. Retire
`stripMarkdown` here and every on-device listener hears `**bold**` and backticks read out
literally. Only the remote leg could benefit, and both legs share the one stream.

So the strip stays until the on-device leg has markdown handling of its own. Anything that
changes is a redesign of who owns text processing, not a deletion.

**Speech calls run on their own HTTP client** (`SpeechModule.provideSpeechRetrofit`, derived from
the shared one) purely to raise `callTimeout` above the server's 30s/60s deadlines. At the shared
30s the client wins the race and replaces a diagnosable `502 speech_gateway_timeout` with a generic
`SocketTimeoutException`. The inherited read timeout is untouched, so a dead socket still fails
fast.

## What is and is not verified

The gateway itself is live and measured (WAV transcribed in 0.24s; synthesis ~7.9s cold, ~2.4s
warm), and the API namespace is deployed. **Nothing on this client has been exercised on physical
hardware** — redroid ships no recognizer and no TTS engine, so on-device speech cannot run there at
all and the remote paths have never been driven end to end from the app.

**`capabilities.<direction>.available` is derived from mount state and configuration with NO health
probe**, so it reports `true` for a gateway that is unreachable or whose credential has expired.
Availability is not reachability — which is exactly why the failure handling above has to be right
rather than merely present.

## Speak-along (`SpeechController` + `SentenceChunker`)

`SpeechController` lives HERE (all speech in `voice/`), with its own tests
(`app/src/test/.../voice/SpeechControllerTest.kt`). `AssistTurnMachine` owns its OWN instance
(constructed in its init) — a SECOND call site of the same shared class, not a fork;
`ui.chat.ChatViewModel` (speak-along + read-aloud) is the first. The two never contend for the same
live turn, because the overlay's in-card first turn always either hands off or tears down before
`ChatViewModel.bind()` attaches to that session.

**Cross-instance handoff law: temporal non-overlap alone doesn't mean nothing needs reconciling.**
`ChatViewModel.bind()`'s history replay unconditionally re-folds the WHOLE persisted transcript through
its own reducer, and that replay used to run through `ChatViewModel`'s live speak-along pipeline
(`publish()` → `speech.onAssistantMessage`) against a brand-new `SpeechController` whose
`closedKey`/`chunker`/`consumed` all start at zero — with no idea the OVERLAY's instance already spoke
that text. Deterministic on a voice-tagged handoff (`activeTurnModality == Voice`), not a race: tapping
Expand mid-turn re-spoke the in-progress reply from word one; a second voice follow-up re-spoke turn
1's already-spoken reply in full.

Fix: `bind()`'s history-replay loop passes `speakAlong = false` (an `applyEvent`/`publish()` parameter,
default `true` — byte-equivalent everywhere else), and once the replay settles,
`SpeechController.primeAlreadySpoken(item, modality)` marks whatever text is now present as already
spoken, using the SAME `SentenceChunker.push`-then-discard "consumed cursor" mechanism
`onAssistantMessage` relies on. **Any FUTURE second call site of `SpeechController` must ask the same
question**: does this instance need priming before its first live fold, or could it re-speak something
a sibling instance already said?

`SentenceChunker` is `SpeechController`'s markdown-stripping/sentence-chunking engine, feeding BOTH
call sites. Its invariants:

- Stable `utteranceId = {messageId}:{sentenceIndex}`; queue with `QUEUE_ADD`.
- On `agent_message` reconciliation: already-spoken text is NEVER re-spoken; only the unspoken
  remainder is corrected.
- Feeds plain text: code fences dropped (one "code block omitted"), links → link text, emphasis
  markers stripped. Tool chips are never spoken.
- Barge-in (new LISTENING turn / Stop / dismiss / `toggleSpeak()` mute) → `SpeechController.bargeIn()` →
  `Synthesizer.stop()` flushes the queue AND latches the barged-in message's key as permanently closed
  for the rest of that turn (`closedKey`) — idempotent, ≤ 200ms. Unmuting via `toggleSpeak()` does NOT
  retroactively resume a barged-in message mid-sentence; it only re-arms speech for assistant text that
  folds in AFTER the unmute.
- Speech never blocks UI state transitions — `Streaming(done = true,...)` is reachable while audio
  still plays; the STATE's own `speaking` field is forced `false` the instant `done` flips true
  regardless of the synthesizer's trailing-utterance tail.
- **Whether a turn may be narrated is `SpeechController.narratesTurn(modality)`, and BOTH
  `onAssistantMessage` and `primeAlreadySpoken` ask it.** A voice turn speaks because the user spoke;
  a TYPED turn speaks only where `DeviceShape.narratesTextTurns` says the modality gate is answering
  the wrong question — on a television, which has no voice entry point at all, so under the handheld
  rule every turn there was silent while the user's read-aloud switch read as ON. Muting is
  deliberately NOT part of the predicate: `onAssistantMessage` must not speak a muted turn, but
  priming must still consume its text.
  **The two callers cannot be split.** A shape that narrates typed turns while priming still gates on
  `modality == Voice` re-speaks a whole reply from word one on the next binding — the cross-instance
  handoff law below, with the modality changed underneath it.
  `SpeechController` takes the shape as an injected collaborator defaulting to `Handheld`, and
  `AssistTurnMachine`'s instance deliberately keeps that default: the overlay is reached through the
  assistant role, which is unreachable on every TV (app-root CLAUDE.md), so exactly ONE instance —
  `ChatViewModel`'s, which injects the real `DeviceShape` — ever narrates a typed turn.

## Fakes are load-bearing, not test sugar

redroid (primary dev device) is AOSP: `SpeechRecognizer` **does not exist** there and no TTS engine
ships. `FakeTranscriber` (scripted Ready→Rms→Partial→Final on a timer) and `FakeSynthesizer` (logged
utterances, timed Started/Done) live in the **debug variant** and are what the entire orb state machine
runs on during development and in UI tests. DI binds per build variant: release → platform impls; debug
→ runtime-switchable via the debug settings toggle. Anything beyond the fakes (real recognizer/TTS,
VoiceInteraction gesture flow) requires the physical Pixel + a human.

`AssistOverlayPreviewActivity` mirrors the auto-listen contract via an `autoListen` intent extra
(default `true`, since `FakeTranscriber` needs no permission grant) — `-e autoListen false` opts into
the non-auto-listen `Ready` path for manual verification without editing code.

## Audio focus & etiquette

`AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK` for speech duration, abandon on stop/done. Respect STREAM_MUSIC
volume. Mic active ONLY in LISTENING — no background capture, no foreground service.

## Haptics (`AuraHaptics`)

`AuraHaptics` interface + `NoOpAuraHaptics` (default, keeps `AssistTurnMachine` JVM-testable) +
`VibratorAuraHaptics` (real device, predefined-effect fallback). Five named moments, each wired at one
exact call site:

- `invocation()` — `AuraSession.onShow()`.
- `transcriptAccepted()` — the voice-`Final` branch only.
- `listeningEnded()` — the a11y "mic is off" cue, at the three sites where capture ends without
  producing a transcript to act on: `cancelListening()`, the silence-timeout body in
  `resetSilenceTimer()`, and the `TranscriberEvent.Error` branch. The glow/waveform alone are invisible
  to blind users. **NOT a fourth site:** a blank `TranscriberEvent.Final` (`event.text.isBlank()`)
  falls straight back to `Ready` firing NEITHER haptic — the recognizer returned a definite empty
  result rather than being cut off.
- `settle()` — successful handoff dispatch in the turn-two-onward branch, AND the first turn's stream
  completing ("settle" no longer means only "handed off").
- `error()` — the `AssistUiState.Error` construction sites.

`di/HapticsModule.kt` resolves the `Vibrator`.

**`android.permission.VIBRATE` is required in the manifest** — without it, `Vibrator.vibrate()`
silently no-ops (no exception, no log): every haptic in the app was dead
because nothing checked. `:app:lintDebug` catches a missing declaration; this is why lint runs at every
wave boundary.

## Verifying the overlay WITHOUT a Pixel (works on redroid)

Two paths, both proven — try the fast loop FIRST:

- **Fast loop**: debug-only `AssistOverlayPreviewActivity` hosts the real `AssistTurnMachine` +
  `AssistOverlayScreen` via Hilt with no role/gesture required. Fast, deterministic — the right first
  stop for any overlay change.
- **Real session**: `adb shell cmd role add-role-holder --user 0 android.app.role.ASSISTANT
  com.mewbo.aura` then `adb shell input keyevent 219` — invokes the actual `VoiceInteractionSession` on
  AOSP; `FakeTranscriber` drives a full scripted voice turn. **Needs a quiet box**: under concurrent
  load from other builds on the same redroid container this path has produced a stray unrelated
  activity + an OOM-kill instead of the real overlay (confirmed not a code bug by a clean re-run).
  `am force-stop` clears the ASSISTANT role holder — re-run `add-role-holder` after every force-stop.

Real mic/TTS/haptics/gesture-over-apps still need the physical Pixel. When driving the real session via
`adb input tap`, measure coordinates from an actual screenshot of the SESSION window, never reuse
preview-activity coordinates — the two hosts' geometry differs
([`ui/overlay/CLAUDE.md`](../ui/overlay/CLAUDE.md) § real-session-vs-preview geometry).

## Timer + init traps

- `onRmsChanged` fires CONTINUOUSLY on real recognizers — never reset a silence timer on `Rms` events,
  only on actual speech evidence (`Partial` changes).
- `PlatformSynthesizer` buffers utterances issued before async TTS init resolves (ordered flush on
  success, per-id `Error` on failure) — a fast `SentenceChunker` outruns engine init.

## VoiceInteraction services (Pixel-only for real speech)

`AuraVoiceInteractionService` / `AuraSessionService` / `AuraSession` (ComposeView in
`onCreateContentView()`) implement the assistant-role overlay (`RoleManager.ROLE_ASSISTANT`). The session
window is a system surface — requires the `android.voice_interaction` meta-data XML and
`BIND_VOICE_INTERACTION` permission on BOTH services. Compose inside a `VoiceInteractionSession` needs
explicit lifecycle owners on the content view (`setViewTreeLifecycleOwner` et al.) — the session is not
an Activity.
