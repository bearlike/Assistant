> ↑ [apps/mewbo_aura/CLAUDE.md](../../../../../../../CLAUDE.md) · [root](../../../../../../../../../CLAUDE.md)

# Aura JVM Test Idioms — app/src/test/

Scope: `app/src/test/java/com/mewbo/aura/` — plain-JVM unit tests (Robolectric is NOT in the
dependency catalog; every test double here is a hand-rolled fake, never a mock of an Android SDK
class). These are project-specific traps found the hard way; check here before re-deriving any of
them.

## `backgroundScope` does NOT run under `advanceUntilIdle()` in this project's kotlinx-coroutines-test version — PROBED, not assumed

Do not trust general kotlinx-coroutines-test documentation/blog posts on this point without
re-verifying against the actual pinned version here: a bare `backgroundScope.launch { ran = 1 };
advanceUntilIdle(); assertEquals(1, ran)` FAILS in this project (verified directly, scratch probe).
`backgroundScope` therefore cannot be the scope for any machine/class-under-test whose coroutines
need to actually execute under `advanceTimeBy`/`advanceUntilIdle`/`runCurrent`.

**House idiom for any machine-owning class with an infinite `init`-block collector** (e.g.
`AssistTurnMachine`'s `speech.speakingKey.collect { ... }`, which by design outlives every single
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
the test scheduler AS Main: `Dispatchers.setMain(StandardTestDispatcher())` in `@Before` (+ `resetMain()`
in `@After`), and `runTest(dispatcher)` per test so `advanceUntilIdle()` runs the VM's `init`/`refresh()`
launches on that ONE shared scheduler. The `machineScope()` idiom above is NOT needed here — it exists
only for a class with an infinite `init`-block collector that would otherwise trip `runTest`'s leak check
(`AssistTurnMachine`). A `ViewModel` whose coroutines all COMPLETE (a one-shot `refresh()` that ends when
the fetch returns) has no such collector, so the plain MainDispatcher idiom suffices. `SessionsViewModelTest`
 is the reference implementation; its KDoc states the distinction explicitly.

## `ScriptedTranscriber`: one script per `listen()` call, never a shared replay-from-zero or a shared consumption cursor

A `Transcriber` test double must model the real `SpeechRecognizer` contract: its event stream ENDS
after a Final/Error result, and `AssistTurnMachine` deliberately leaves that finalized collector
draining rather than cancelling it (nothing forces early termination of a completed capture). A test
needing TWO separate capture turns (e.g. a first-then-second-interaction scenario) must give each
`startListening()` call its OWN script, not reuse one shared script list across two `.listen()`
invocations:

- **Shared replay-from-zero** (a `flow { for (event in script) {...} }` rebuilt fresh on every
  `.listen()` call, referencing the SAME list) — the FIRST fix attempt's failure mode: the SECOND
  `startListening()` call restarts the same script from index 0, so it emits the FIRST turn's events
  again instead of the second turn's. A real second capture's events are silently swallowed by
  replaying stale ones.
- **Shared consumption cursor** (a single mutable index into one script list, advanced as events are
  emitted) — the SECOND fix attempt's failure mode: the first capture's still-draining collector (see
  above — it's never cancelled) keeps consuming from the SAME cursor concurrently with the second
  capture's fresh collector, so the second turn's events get consumed-and-discarded by the wrong
  collector instead of ever reaching the second capture's own consumer.

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
NUMERIC offset, e.g. `2026-07-02T03:34:42.633140+00:00`. `java.time.Instant.now().toString()`
produces a BARE `Z` suffix instead, which happens to still parse fine through
`Timestamps.parseInstantOrNull` — meaning a naive test fixture built from `Instant.now().toString()`
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
this shipped as three separately-and-silently-broken copies before being unified (`data/CLAUDE.md`,
` `). Never reintroduce a second, local `Instant.parse` call anywhere in this codebase; route
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
