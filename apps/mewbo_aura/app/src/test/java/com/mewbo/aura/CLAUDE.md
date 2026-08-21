> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../CLAUDE.md) · [root](../../../../../../../../../CLAUDE.md)

# Aura JVM Test Idioms — app/src/test/

Scope: `app/src/test/java/com/mewbo/aura/` — unit tests. These are project-specific traps found the
hard way; check here before re-deriving any of them.

**Plain JVM is the default and stays the default.** Almost every double here is a hand-rolled fake
rather than a mock of an Android SDK class, and almost every suite runs with no Android on the
classpath at all. Robolectric IS in the catalog now, but it is the EXCEPTION, taken only where the
behaviour under test is an Android object doing its job:

| Suite | Why it needs Android |
|---|---|
| `ui/control/DeviceControlOverlayTest` | the whole behaviour IS adding and removing a `WindowManager` window |
| `ui/chat/ChatTranscriptDisclaimerTest`, `ui/composer/ComposerPrimaryActionTest`, `ui/settings/SettingsRowRenderTest` | Compose semantics — the claim is what RENDERS, not what a predicate returns |

Reach for it only when a pure test structurally cannot observe the failure — a fold cannot see the
absence of a window. A Robolectric class pays a real per-class setup cost, so anything expressible
as a pure function belongs in a plain-JVM suite. `ui/chat/ChatViewModelTest` is the worked example
of how far plain JVM reaches: a whole ViewModel over a faked HTTP/SSE seam, no Android runner.

## Under Robolectric a frame loop HANGS the suite — it does not fail it

The worst shape a gate can take, because it reads as a slow suite rather than a bug. Measured: a
worker pinned at 122% CPU for ten minutes, no timeout, no output.

**The one rule behind three symptoms: the Compose test framework suspends only the INFINITE-ANIMATION
clock, so any bare frame or delay loop escapes it.**

- `ShadowChoreographer.isPaused` is `false` by default, which posts vsync callbacks at zero delay.
  Any surface carrying an aurora/orb shader drives an unbounded frame loop, so `ShadowLooper.idle()`
  drains a queue that refills itself and never returns. `setPaused(true)` + `setFrameDelay(16ms)` in
  `@Before` starves every frame source regardless of which loop produced it, and makes `idleFor` a
  bounded number of frames. This is the general cure; the two below are the same defect seen closer.
- `RmsWaveform` runs a bare `while (true) { withFrameNanos { … } }`. Unlike
  `withInfiniteAnimationFrameMillis` (what `ShaderFrameClock` uses, and what `InfiniteAnimationPolicy`
  suspends), it never yields an idle frame — so `waitForIdle()` spins. Test `ComposerState.Dictation`
  with a non-null `partialText`, taking the transcript branch instead of the bars.
- `rememberStreamedText` runs an unbounded `while (true) { … delay(…) }` while `isStreaming`. Express
  "no settled reply" as a transcript with no assistant message at all — the same input to the gate.

## Text measures at 1px/char here, so a width assertion over text may have NO power to fail

Two facts, both measured with throwaway probes, and the second explains every confusing reading the
first produces.

**1. The text substrate is degenerate, uniformly.** Reading `TextLayoutResult.size` from
`onTextLayout` — the text box itself, not a node's bounds — one 23-character string rendered at three
styles in one composition came back **23 × 35, one line, for all three**:

```
sectionHeader  14sp (lineHeight 18sp) → 23 x 35
listItem       16sp (lineHeight 23sp) → 23 x 35
caption        12sp (lineHeight 17sp) → 23 x 35
```

One pixel per character and ONE identical height across three different line heights, at
`density=1.0`.

**The mechanism is not a font problem at all — Robolectric's `Paint` does not measure text.**
Disassembled from `shadows-framework-4.16.1.jar`:

```
protected float measureText(java.lang.String);
   1: aload_1
   2: invokevirtual  // Method java/lang/String.length:()I
   5: i2f
   6: invokespecial  // Method applyTextScaleX:(F)F
```

It returns the CHARACTER COUNT. `GraphicsModeConfigurer` defaults to `Mode.LEGACY` — "shadows that
are no-ops and fakes" — and nothing in this module sets `@GraphicsMode`, so every text measurement
here is `text.length()`. That is why the number is exactly 1.000 and not 1.02: **a measurement
landing on an exact round value is a code path, not data.** Chasing the magnitude cost four dead
font-shaped hypotheses (per-style resolution, async loading, layout slot, missing resource); reading
the shadow source settled it in two files.

**2. `boundsInRoot` reports the LAYOUT SLOT, not the text box.** This is what makes the first fact
easy to misdiagnose. Same 23-character string, one composition:

```
bare Text (sizes to content)                    →  23px
Text.fillMaxWidth() in Column(weight(1f)) in Row → 309px
```

So a "sane-looking" width is not evidence that metrics work somewhere — it is a weighted slot's
width being reported. One such reading (191px, from a mutated header) was chased as a harness
anomaly for hours; it was an allocation, and it shrank by exactly the badge's extra width, which the
arithmetic showed once anyone compared the two deltas.

**The consequence for writing tests here.** A bounds comparison over text can be true of the slots
while saying nothing about the glyphs. Two rules:

- **Assert the CONTROL render is non-degenerate before trusting any comparison against it.** A
  comparison between two degenerate renders passes with zero power to fail — the same disease as a
  missing permission request, just wearing an assertion instead of an absence.
- **Read `TextLayoutResult.size` when the claim is about TEXT**; `boundsInRoot` when the claim is
  about layout. They are different questions and only one of them is about the font.

**The cure: `@GraphicsMode(GraphicsMode.Mode.NATIVE)`, and it targets `METHOD`** — so a test that
genuinely needs real text gets it without imposing native graphics on every Robolectric suite in the
module. Measured, same fixture, both modes:

```
LEGACY  title (23 chars, 14sp) → 23.0 x 35.0    ← text.length()
NATIVE  title (23 chars, 14sp) → 150.0 x 17.0   ← a real advance width
```

`SettingsRowRenderTest`'s squeeze test carries that annotation for exactly this reason. Under the
default it was comparing `23 == 23` and would have passed however narrow the allocation became; its
earlier red came from a mutation that changed the node's SIZING MODE (`weight` defaults to
`fill = true`), not from the squeeze it names. **A red is not proof of power — check WHY it went
red.** A sibling test asserting the same law over `SettingsRow` was written, went green, and was
deleted before anyone noticed; that one had no control assertion to catch it.

**One coverage gap this leaves, worth knowing:** under LEGACY every item composes, because a lazy
list decides what fits from measured heights and the stub makes everything the same small size. So
**no test at the default mode can catch a virtualisation regression**, and a node-count assertion
over a `LazyColumn` is only safe while the fixture is small enough that virtualisation never engages
— a property of the fixture, not of the assertion.

## `mockito-core` is no longer only for `android.net.Uri`

A Robolectric test needing a real `SettingsStore` must mock `KeystoreCipher`: its constructor calls
`KeyStore.getInstance("AndroidKeyStore")` and Robolectric ships no such JCA provider, so it throws
`NoSuchAlgorithmException`. `SettingsStore` itself stays real.

## A post-restore green can be FAKE — `--rerun-tasks` on the confirming run

When proving a test can fail, restoring the production file byte-identically makes the task inputs
identical too, so Gradle serves `:app:testPublicDebugUnitTest` `FROM-CACHE` and hands back the
**pre-mutation** XML — same content, same timestamp. Measured; it will happen every time. The
confirming run needs `--rerun-tasks`, and the check is the XML `timestamp` attribute, not the count.

Worked example of the check discriminating rather than merely being asserted — a red→green cycle run
WITHOUT `--rerun-tasks`, where every set advanced and so every run genuinely executed:

```
baseline green   00:28:22 / 00:28:30 / 00:28:31
RED (flipped)    00:29:41 / 00:29:48 / 00:29:50
green (restored) 00:30:42 / 00:30:49 / 00:30:51
```

A cache hit would have replayed the 00:28 timestamps verbatim.

## `backgroundScope` does NOT run under `advanceUntilIdle()` in this project's kotlinx-coroutines-test version — PROBED, not assumed

Do not trust general kotlinx-coroutines-test documentation/blog posts on this point without
re-verifying against the actual pinned version here: a bare `backgroundScope.launch { ran = 1 };
advanceUntilIdle; assertEquals(1, ran)` FAILS in this project (verified directly, scratch probe).
`backgroundScope` therefore cannot be the scope for any machine/class-under-test whose coroutines
need to actually execute under `advanceTimeBy`/`advanceUntilIdle()`/`runCurrent`.

**House idiom for any machine-owning class with an infinite `init`-block collector** (e.g.
`AssistTurnMachine`'s `speech.speakingKey.collect {... }`, which by design outlives every single
test body — production parity, since `AuraSession`'s real scope lives for the whole session):

```kotlin
private fun TestScope.machineScope(): CoroutineScope =
    CoroutineScope(StandardTestDispatcher(testScheduler) + SupervisorJob())
```

Why this exact shape and not the two more obvious alternatives:
- Plain `this` (the `TestScope` itself) DOES run its coroutines under `advanceUntilIdle()`, but
  `runTest`'s own leak check then throws `UncompletedCoroutinesError` at test-body end, because the
  infinite collector is a genuine, permanent CHILD of the test's own job and by definition never
  completes.
- `backgroundScope` avoids the leak check (jobs launched there ARE exempted from it) but — per the
  probe above — its queued work never actually executes under this project's `advanceUntilIdle()`,
  which starves every machine job that needs virtual time to progress (send dispatch, silence
  timers, scripted stream events — everything).
- An INDEPENDENT `CoroutineScope` built on the SAME `testScheduler` (shared with the enclosing
  `TestScope`) gets both halves right at once: `advanceUntilIdle()` still drives it, because it's on
  the same scheduler; `runTest`'s completion/leak check ignores it, because it's not a structural
  child of the test coroutine at all. Each test constructs its own `machineScope()` instance — no
  cross-talk between tests, the JVM just garbage-collects each one when the test method returns.

Any NEW test file for a class with this same "infinite background collector by design" shape should
reach for this exact idiom rather than reinventing it — `AssistTurnMachineTest.kt` is the reference
implementation.

## Testing a `ViewModel` (only `viewModelScope` launches) — plain `setMain`, NOT `machineScope()`

`viewModelScope` dispatches on `Dispatchers.Main`, so a `@HiltViewModel` test drives it by installing
the test scheduler AS Main: `Dispatchers.setMain(StandardTestDispatcher)` in `@Before` (+ `resetMain()`
in `@After`), and `runTest(dispatcher)` per test so `advanceUntilIdle()` runs the VM's `init`/`refresh()`
launches on that ONE shared scheduler. The `machineScope()` idiom above is NOT needed here — it exists
only for a class with an infinite `init`-block collector that would otherwise trip `runTest`'s leak check
(`AssistTurnMachine`). A `ViewModel` whose coroutines all COMPLETE (a one-shot `refresh()` that ends when
the fetch returns) has no such collector, so the plain MainDispatcher idiom suffices. `SessionsViewModelTest`
  is the reference implementation; its KDoc states the distinction explicitly.

## A class under test that hops to `Dispatchers.IO` is NOT driven by `advanceUntilIdle()`

`advanceUntilIdle()` drains the TEST SCHEDULER. A suspend function that does real work inside
`withContext(Dispatchers.IO)` has left that scheduler entirely — it is on a genuine thread pool — so
the scheduler goes idle while the work is still running, and an assertion right after
`advanceUntilIdle()` reads the state from BEFORE it finished. **This passes or fails by timing**,
which is the worst shape a gate can take: green locally, red on a loaded machine, and neither result
means anything.

Injecting an `UnconfinedTestDispatcher`-backed scope does NOT fix it. Unconfined only means the
coroutine starts eagerly in the caller's thread; the `withContext(Dispatchers.IO)` inside still hops.
So a class whose `check()` never leaves the scheduler completes synchronously inside the triggering
call, while its `download()` on the same scope does not — the same object, two different rules.

`data/update/AppUpdateRepositoryTest` is the worked example: `AppUpdateRepository.fetch()` streams a
file inside `withContext(Dispatchers.IO)`, so the suite polls WALL-CLOCK time for the terminal state
(`withContext(Dispatchers.Default) { delay(5) }` in a bounded loop) rather than virtual time. Two
rules make that poll honest rather than a sleep-and-hope:

- **Bound it**, so a hang fails the test instead of wedging the suite.
- **Assert the terminal state's own FIELDS afterwards**, never just that the state changed. A helper
  that returned one state too early then fails the very next assertion, instead of passing silently
  on an intermediate value.

## `ScriptedTranscriber`: one script per `listen()` call, never a shared replay-from-zero or a shared consumption cursor

A `Transcriber` test double must model the real `SpeechRecognizer` contract: its event stream ENDS
after a Final/Error result, and `AssistTurnMachine` deliberately leaves that finalized collector
draining rather than cancelling it (nothing forces early termination of a completed capture). A test
needing TWO separate capture turns (e.g. a first-then-second-interaction scenario) must give each
`startListening()` call its OWN script, not reuse one shared script list across two `.listen()`
invocations:

Two shapes that look right and are not:

- **Shared replay-from-zero** (a `flow { for (event in script) {...} }` rebuilt fresh on every
  `.listen()` call, referencing the SAME list) — the SECOND `startListening()` restarts the script
  from index 0 and emits the FIRST turn's events again, silently swallowing the real second capture.
- **Shared consumption cursor** (a single mutable index into one script list, advanced per emitted
  event) — the first capture's still-draining collector (never cancelled) keeps consuming from the SAME
  cursor concurrently with the second capture's fresh collector, so the second turn's events get
  consumed-and-discarded by the wrong collector.

**Working shape**: `vararg scripts: List<Pair<Long, TranscriberEvent>>`, with a private `nextScript`
index incremented once per `.listen()` CALL (not per emitted event) — each call gets its own
independent script, genuinely modeling a fresh recognizer session:

```kotlin
private class ScriptedTranscriber(private vararg val scripts: List<Pair<Long, TranscriberEvent>>) : Transcriber {
    private var nextScript = 0
    override fun listen(): Flow<TranscriberEvent> = flow {
        val script = scripts.getOrNull(nextScript++) ?: return@flow
        for ((delayMs, event) in script) {
            delay(delayMs)
            emit(event)
        }
    }
}
```

A test scripting only ONE `startListening()` call is unaffected either way — this only matters once
a test needs a genuine second capture turn within the same test body.

## `android.net.Uri` cannot be hand-doubled in a plain-JVM test

Package-private constructor, real methods throw "not mocked" outside Robolectric/instrumentation —
the dependency catalog carries `mockito-core` SOLELY for tests that need a behaving `Uri` (e.g.
attachment/staged-file tests). Don't reach for a hand-rolled fake `Uri` subclass; it won't compile
or won't behave. Prefer avoiding real `Uri` construction in test fixtures where the test doesn't
actually need URI semantics (a plain string id is often enough); reach for Mockito only when a real
`Uri` API surface (`getPath`, `toString`, equality) genuinely matters to the assertion.

## Backend timestamps: numeric offset (`+00:00`), never bare `Z`

The real backend emits Python `datetime.isoformat()` — microsecond precision plus an explicit
NUMERIC offset, e.g. `2026-07-02T03:34:42.633140+00:00`. `java.time.Instant.now.toString`
produces a BARE `Z` suffix instead, which happens to still parse fine through
`Timestamps.parseInstantOrNull` — meaning a naive test fixture built from `Instant.now.toString`
can mask a real regression in that parser's numeric-offset path without ever failing. Any test
fixture standing in for a backend-supplied timestamp (`SessionSummary.updatedAt`/`createdAt`, event
`ts` fields) should use the SAME numeric-offset shape the live device actually sends, e.g.:

```kotlin
val recentIso = java.time.Instant.now().toString().removeSuffix("Z") + "+00:00"
```

## `Instant.parse` accepts numeric offsets on desktop JVMs but throws on Android's bundled `java.time`

`Instant.parse` (`DateTimeFormatter.ISO_INSTANT`) is spec'd to accept ONLY a literal `Z` — desktop
JVMs are more lenient about a numeric offset form (`+00:00`) than Android's actual bundled `java.time`
is at runtime, so a naive `Instant.parse(backendTimestamp)` can pass in a local/CI JVM unit test
while throwing on a real device. `Timestamps.parseInstantOrNull` (`data/model/Timestamps.kt`) is the
ONE shared parser for this reason — it tries `OffsetDateTime.parse` (accepts both `Z` and a numeric
offset via `ISO_OFFSET_DATE_TIME`) FIRST, falling back to `Instant.parse` only for a bare
no-offset instant string. Every ts-comparison call site (`TranscriptReducer`'s echo window,
`ui/sessions/RelativeTime`, `AssistTurnMachine.recentSessionOrNull`) goes through this ONE function —
it shipped as three separately-and-silently-broken copies before being unified
([`data/model/CLAUDE.md`](../../../../../main/java/com/mewbo/aura/data/model/CLAUDE.md)). Never
reintroduce a second, local `Instant.parse` call anywhere in this codebase; route
through `Timestamps.parseInstantOrNull` even in a test fixture, so a JVM-only test can't mask a
real device-only failure mode the way a bare `Instant.parse` naively would.

## Contract-testing scripted/mock wire content: round-trip through the REAL parser, never assert the fixture against itself

`mock/MockScenariosTest.kt` is the reference shape for testing ANY hand-built wire fixture (a mock
backend's scripted frames, a hardcoded SSE-replay fixture, etc.): decode every scripted frame
through `SessionEvent.decode` - the SAME parser a genuine SSE connection feeds - and assert on the
DECODED domain objects, never on the raw JSON the fixture itself constructed. A frame that decodes
to `SessionEvent.Unknown` (a typo'd `type`, a wrong `@SerialName`) still "passes" a test that only
inspects the fixture's own `JsonObject` tree, and still renders as a silent no-op on-device (the
reducer just drops `Unknown` events) - decoding through the real parser is what turns that into an
actual test failure. Two corollaries this suite encodes: (1) never invent a wire shape the real
backend can't produce even for a "test-only" fixture - `data/CLAUDE.md`'s "no synthetic error event
type" rule applies here exactly as it does to production code, so a scripted failure must surface
via `completion.error`, never a fake `"type":"error"` frame; (2) assert `depth: 0` on any scripted
`agent_message`/`agent_message_delta` frame explicitly - `TranscriptReducer` silently drops
non-root-depth narration, so a wrong depth value produces an empty-looking reply on-device with no
test failure unless the depth field itself is asserted.
