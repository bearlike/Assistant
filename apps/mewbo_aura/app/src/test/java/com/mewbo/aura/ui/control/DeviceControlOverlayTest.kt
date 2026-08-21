package com.mewbo.aura.ui.control

import android.content.Context
import android.os.Looper
import android.view.View
import android.view.WindowManager
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.DeviceShape
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.settings.KeystoreCipher
import com.mewbo.aura.data.settings.SettingsStore
import java.time.Duration
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mockito
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowChoreographer
import org.robolectric.shadows.ShadowSettings
import org.robolectric.shadows.ShadowWindowManagerImpl

/**
 * The overlay's WINDOW lifecycle, asserted against a real `WindowManager`.
 *
 * **Why this one suite is not plain-JVM.** Everything else in `ui/control/` is a pure fold or a
 * pure script and is tested with no Android on the classpath at all — which is correct, and is also
 * how this surface shipped completely invisible with every gate green: `raise()`'s one early
 * `return` on [android.provider.Settings.canDrawOverlays] is not a decision any pure test can see,
 * because the thing it decides is whether a window exists. `DeviceControlNarrationTest` and
 * `VeilFadeTest` cover what the code COMPUTES; this covers what it PUTS ON SCREEN.
 *
 * **What it can and cannot prove.** Robolectric's `WindowManager` records `addView`/`removeView`,
 * so "a window was added", "it was taken away", and "it left and came back" are real assertions.
 * Nothing here renders: no pixel is produced, the AGSL shader never draws, and a glow that composes
 * to a fully transparent surface would pass every test below. Window presence is the claim.
 *
 * **Determinism comes from two knobs, and without the first this suite hangs forever.**
 * [ShadowChoreographer.setPaused] is false by default, which posts vsync callbacks at zero delay —
 * and `AuroraEdgeGlow` drives an unbounded frame loop, so `ShadowLooper.idle()` drains a queue that
 * refills itself and never returns (measured: a worker spinning at 122% CPU with no timeout).
 * Paused, a frame lands only when virtual time is advanced past it, so every wait below is a bounded
 * number of frames rather than a race. The second knob is that all timing is virtual:
 * `idleFor(...)` advances Robolectric's clock, so nothing here sleeps and the durations are read
 * off the production constants rather than restated.
 */
@RunWith(RobolectricTestRunner::class)
// Pinned rather than inherited from `targetSdk`: this is the newest SDK whose `android-all` jar the
// build already has, so the gate never depends on a download. Nothing under test is SDK-conditional
// above it — `TYPE_APPLICATION_OVERLAY` is API 26+ and the obscuring-alpha ceiling is API 31+.
@Config(sdk = [34])
class DeviceControlOverlayTest {

    private lateinit var harness: OverlayHarness

    @Before
    fun setUp() {
        // See the class KDoc: unpaused, the shader's frame loop makes every `idle()` non-terminating.
        ShadowChoreographer.setPaused(true)
        ShadowChoreographer.setFrameDelay(Duration.ofMillis(FRAME_MS))
    }

    @After
    fun tearDown() {
        if (this::harness.isInitialized) harness.dispose()
    }

    /**
     * The permission gate is honest: no `SYSTEM_ALERT_WINDOW`, no window.
     *
     * This is the exact shape of the defect that shipped — `canDrawOverlays` false on every fresh
     * install, `raise()` returning early, and the whole surface silently absent while device control
     * worked perfectly. The assertion that matters is the pair: NO window, and no throw either, so
     * the grant is never blocked by the surface announcing it.
     */
    @Test
    fun `a grant with no overlay permission adds no window and does not fail`() {
        harness = OverlayHarness(canDrawOverlays = false)

        harness.takeControl()

        assertEquals(emptyList<View>(), harness.windows)
        assertTrue("the grant must be held even with nothing announcing it", harness.grantHeld())
    }

    /** With the permission, the same grant raises both windows — the decoration and the Stop pill. */
    @Test
    fun `a grant with the overlay permission raises both windows`() {
        harness = OverlayHarness(canDrawOverlays = true)

        harness.takeControl()

        assertEquals(2, harness.windows.size)
    }

    /**
     * On a television the Stop pill window is NOT put up — and the decoration still is.
     *
     * Both windows carry `FLAG_NOT_FOCUSABLE`, which is what lets the agent's injected input reach
     * the app underneath, and a window with that flag receives no key events at all. A finger does
     * not need any; a D-pad has nothing else, so on that shape the pill was a control drawn above
     * everything and pressable by nobody. The pair is the assertion: the announcement survives, and
     * the dead affordance does not appear.
     *
     * This is the one thing a test here CAN see about that change. It cannot see that the pill was
     * unpressable in the first place — that is a window-flag fact about a real input dispatcher, and
     * nothing under Robolectric dispatches a key to a window.
     */
    @Test
    fun `a television raises the decoration but not the unpressable stop pill`() {
        harness = OverlayHarness(canDrawOverlays = true, shape = DeviceShape.Television)

        harness.takeControl()

        assertEquals(1, harness.windows.size)
        assertTrue("the grant is still held — the surface must never block it", harness.grantHeld())
    }

    /**
     * A television teardown still removes its one window, and does not lose the ease-off doing it.
     *
     * `lower()` names its windows by ROLE rather than by position for exactly this case: with no
     * pill the list holds one entry, and under `first()`/`last()` the decoration would answer to
     * both and be torn out on the PILL's short timer — the abrupt on->off luminance change the long
     * exit exists to prevent. Asserting it is still up just after the pill's own dismiss is what
     * pins that; asserting it is gone at the end is what pins that it does leave.
     */
    @Test
    fun `a television decoration outlives the pill timer and is still removed`() {
        harness = OverlayHarness(canDrawOverlays = true, shape = DeviceShape.Television)
        harness.takeControl()
        assertEquals(1, harness.windows.size)

        val decoration = harness.windows.single()

        harness.beginRelease()

        // Past the PILL's timer by a clear margin. A `lower()` that still addressed its windows by
        // position would have removed this one here, on a timer that belongs to a window this shape
        // never put up.
        harness.advanceTo((PILL_EXIT_MS + FULL_EXIT_MS) / 2L)
        assertEquals(
            "the decoration must not come down on the pill's timer",
            listOf(decoration),
            harness.windows.toList(),
        )

        harness.advanceTo(FULL_EXIT_MS + 4 * FRAME_MS)
        assertTrue("the decoration must still leave", harness.windows.isEmpty())
    }

    /**
     * The grant is the lifetime, in both directions.
     *
     * Prose in `ui/control/CLAUDE.md` claims the windows span the grant and nothing else; this is
     * the half of that claim a test can hold.
     */
    @Test
    fun `releasing the grant lowers the overlay`() {
        harness = OverlayHarness(canDrawOverlays = true)
        harness.takeControl()
        assertEquals(2, harness.windows.size)

        harness.releaseControl()

        assertEquals(emptyList<View>(), harness.windows)
    }

    /**
     * **A grant can end with nobody calling `stop()`** — the Shizuku binder dies with its host
     * process and the grant demotes itself, which only a collector sees.
     *
     * The class documents this as the entire reason the `init` collector exists rather than a
     * raise/lower pair on the call sites that take and release control. Nothing pinned it: a version
     * that lowered the windows from `stop()` alone would pass every other test here and leave a
     * window claiming the phone can be driven after the channel behind it is gone.
     *
     * Note what is NOT called below — the substrate simply stops being ready.
     */
    @Test
    fun `a grant ending with nobody calling stop still lowers the overlay`() {
        harness = OverlayHarness(canDrawOverlays = true)
        harness.takeControl()
        assertEquals(2, harness.windows.size)

        harness.killTheBinder()

        assertEquals(emptyList<View>(), harness.windows)
        assertFalse("the grant itself must be gone, not just the window", harness.grantHeld())
    }

    /**
     * The escape hatch removes the windows AND releases the grant — both halves, or it is not one.
     *
     * `forceTeardown()` exists for the state where the cooperative teardown did not cooperate, so
     * the assertion pair matters more here than anywhere else on this surface: removing the windows
     * while leaving the grant held would leave an agent driving the device with nothing on screen
     * saying so — the toggle-that-lies failure, caused by the very thing meant to cure it. Releasing
     * the grant while leaving a window up is the wedge it is escaping.
     *
     * No time is advanced between the call and the assertion, and that is the point: unlike
     * [DeviceControlOverlay.lower] this must not wait out the exit fade.
     */
    @Test
    fun `forceTeardown removes every window and releases the grant at once`() {
        harness = OverlayHarness(canDrawOverlays = true)
        harness.takeControl()
        assertEquals(2, harness.windows.size)
        assertTrue(harness.grantHeld())

        harness.forceTeardown()

        assertTrue("no window may survive the escape hatch", harness.windows.isEmpty())
        assertFalse("the grant must go with them", harness.grantHeld())
    }

    /**
     * It is safe with nothing on screen, and does not poison the surface for the next grant.
     *
     * The second half is the one worth pinning. `forceTeardown` runs on the same dispatcher as the
     * `grant.active` collector that owns every raise and lower; an implementation that cancelled
     * that collector to stop an in-flight `lower()` would pass every assertion above and leave the
     * app unable to ever announce device control again — silently, and only on the SECOND grant.
     */
    @Test
    fun `forceTeardown is safe with nothing up and a later grant still raises`() {
        harness = OverlayHarness(canDrawOverlays = true)

        harness.forceTeardown()
        assertTrue(harness.windows.isEmpty())

        harness.takeControl()
        assertEquals("a later grant must still raise the surface", 2, harness.windows.size)
    }

    /**
     * **The teardown OUTLIVES the fade, in two stages — and each window comes down at its own one.**
     *
     * `lower()` flips `showing` false and then waits: the pill's window is removed after its own
     * short dismiss, the decoration's only after [DEVICE_CONTROL_EXIT_MS]. Removing a lit surface
     * outright is the abrupt on→off luminance change the photosensitivity law forbids, and a
     * teardown shorter than an animation still in flight rips the window out mid-fade — the exact
     * cut the fade exists to remove.
     *
     * **Why the intermediate samples and not just "eventually gone".** The suite's other lowering
     * tests settle past every wait before looking, so they would pass verbatim against the abrupt
     * teardown this replaced. Each sample below is a different failure:
     * - before the pill's dismiss: an abrupt teardown has already removed both;
     * - between the two: a single-stage teardown has removed both or neither;
     * - one millisecond before the full exit: a decoration removed on the PILL's timer is gone, and
     *   so is one whose wait was restated as a literal that no longer matches the tokens.
     *
     * The windows are compared by identity, never by count alone — "one window left" is also true
     * of a build that removed the wrong one, which would leave a touchable Stop pill over an app the
     * user is already reaching past while the thing it announces has stopped.
     */
    @Test
    fun `the pill leaves first and the decoration outlives the whole exit`() {
        harness = OverlayHarness(canDrawOverlays = true)
        harness.takeControl()
        val raised = harness.windows.toList()
        assertEquals(2, raised.size)
        val decoration = raised.first()

        harness.beginRelease()

        harness.advanceTo(PILL_EXIT_MS - 1L)
        assertEquals(
            "no window may be torn out while its own dismiss is still playing",
            raised,
            harness.windows.toList(),
        )

        harness.advanceTo((PILL_EXIT_MS + FULL_EXIT_MS) / 2L)
        assertEquals(
            "the pill's WINDOW goes, not just its opacity — a faded window still owns its touches",
            listOf(decoration),
            harness.windows.toList(),
        )

        harness.advanceTo(FULL_EXIT_MS - 1L)
        assertEquals(
            "the decoration must still be on screen for the whole ease-off",
            listOf(decoration),
            harness.windows.toList(),
        )

        harness.advanceTo(FULL_EXIT_MS + SETTLE_MS)
        assertEquals(emptyList<View>(), harness.windows)
    }

    /**
     * `hiddenDuring` restores the windows even when the block throws.
     *
     * The throwing path is the one the user is looking at: a `FLAG_SECURE` screen makes the capture
     * fail, and if that stranded the windows the user would be left with an agent driving their
     * phone and nothing on screen saying so. The KDoc calls the `finally` + `NonCancellable`
     * load-bearing; this is the assertion behind it.
     *
     * Asserted on VISIBILITY rather than on window alpha deliberately. `View.visibility` is written
     * directly on the view, so it cannot silently no-op; the alpha ramp goes through
     * `updateViewLayout` inside a `runCatching`, which would swallow a failure and turn an alpha
     * assertion into one that cannot fail.
     */
    @Test
    fun `hiddenDuring restores the windows when the block throws`() {
        harness = OverlayHarness(canDrawOverlays = true)
        harness.takeControl()

        val capture = harness.beginCapture()
        assertTrue(
            "the windows must be out of the frame before the capture runs",
            harness.windows.all { it.visibility == View.INVISIBLE },
        )

        capture.failWith(IllegalStateException("FB is protected: PERMISSION_DENIED"))

        assertEquals(2, harness.windows.size)
        assertTrue(
            "a thrown capture must never strand the announcement off screen",
            harness.windows.all { it.visibility == View.VISIBLE },
        )
    }

    /**
     * Everything one of these tests needs to stand up a real [DeviceControlOverlay] over a real
     * `WindowManager`, plus the four ways a test drives it.
     *
     * Every collaborator is the production type: the grant is a real [DeviceControlSession] over
     * fake seams, so "the binder died" below is the genuine `HELD -> LOST` transition rather than a
     * flag a fake overlay was told about. The one exception is [KeystoreCipher], which is mocked
     * because its constructor opens the `AndroidKeyStore` JCA provider and Robolectric ships none —
     * `SettingsStore` itself is real, and the path under test (`reducedMotion`) never reaches the
     * cipher.
     */
    private class OverlayHarness(
        canDrawOverlays: Boolean,
        shape: DeviceShape = DeviceShape.Handheld,
    ) {
        private val context: Context = RuntimeEnvironment.getApplication()
        private val status = MutableStateFlow<DeviceControlStatus>(DeviceControlStatus.Ready)
        private val scope = CoroutineScope(Dispatchers.Main.immediate + SupervisorJob())
        private val grant = DeviceControlSession({ status }, { true }, scope)

        init {
            ShadowSettings.setCanDrawOverlays(canDrawOverlays)
        }

        private val overlay = DeviceControlOverlay(
            context = context,
            grant = grant,
            // `follow()` is never called, so narration never starts and the repository is never
            // dereferenced. Throwing rather than mocking makes that an assertion: a change that
            // starts a subscription without a session id fails here loudly.
            runs = dagger.Lazy<RunRepository> {
                error("RunRepository must not be needed to raise or lower the overlay")
            },
            settings = SettingsStore(context, Mockito.mock(KeystoreCipher::class.java)),
            foreground = AppForegroundChecker { true },
            shape = shape,
            scope = scope,
        )

        private val shadowWindows = Shadow.extract<ShadowWindowManagerImpl>(
            context.getSystemService(Context.WINDOW_SERVICE) as WindowManager,
        )

        /** The windows currently on screen, in the order they were added. */
        val windows: List<View> get() = shadowWindows.views

        fun grantHeld(): Boolean = grant.isActive()

        /** Take control the way `device_control_start` does, and let the raise land. */
        fun takeControl() {
            scope.launch { grant.start() }
            settle()
        }

        /** The Settings debug escape hatch. Settles so the launched teardown actually runs. */
        fun forceTeardown() {
            overlay.forceTeardown()
            settle()
        }

        /** End it the way the Stop pill and the notification do. */
        fun releaseControl() {
            grant.stop()
            settle()
        }

        /** Virtual milliseconds since [beginRelease], so a test names an ABSOLUTE point on the
         * teardown timeline instead of accumulating deltas that drift as the constants move. */
        private var sinceRelease = 0L

        /**
         * End the grant and stop at the instant the teardown begins.
         *
         * `idle()` rather than `idleFor(...)`: it runs whatever is already due WITHOUT advancing the
         * clock, so `lower()` reaches its first wait and the timeline below starts at a true zero. A
         * version that advanced even one millisecond here would shift every sample by it.
         */
        fun beginRelease() {
            grant.stop()
            shadowOf(Looper.getMainLooper()).idle()
            sinceRelease = 0L
        }

        /** Advance to [ms] after [beginRelease]. Monotonic by construction — a test that asked to
         * go backwards would be reading a timeline that never happened. */
        fun advanceTo(ms: Long) {
            require(ms >= sinceRelease) { "the teardown timeline only moves forward" }
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(ms - sinceRelease))
            sinceRelease = ms
        }

        /**
         * The Shizuku server going away underneath a live grant — no `stop()`, no tool call, no
         * screen event. The real [DeviceControlSession] demotes `HELD` to `LOST` off its own status
         * collector, which is what the overlay is supposed to be watching.
         */
        fun killTheBinder() {
            status.value = DeviceControlStatus.NotRunning
            settle()
        }

        /** Enter `hiddenDuring` and stop inside the block, with the hide ramp already complete. */
        fun beginCapture(): Capture {
            val capture = Capture()
            scope.launch { runCatching { overlay.hiddenDuring { capture.await() } } }
            settle()
            return capture
        }

        fun dispose() {
            scope.cancel()
        }

        /**
         * A capture whose ending the test chooses, once it has looked at the screen.
         *
         * Nested so that ending one also drives the restore ramp to completion: the fade back is
         * virtual-time work on the same looper, and a test that had to remember to idle afterwards
         * would pass or fail on whether somebody remembered.
         */
        inner class Capture {
            private val finished = CompletableDeferred<Throwable>()

            suspend fun await(): Nothing = throw finished.await()

            fun failWith(error: Throwable) {
                finished.complete(error)
                settle()
            }
        }

        /**
         * Advance virtual time past every wait this surface can be in the middle of.
         *
         * Derived from the production constants rather than restated, so a change to the dismiss
         * fade or the veil cannot leave a test idling for less than the code waits — the failure
         * mode a hardcoded number produces is an intermittent one, which is worse here than no test.
         */
        private fun settle() {
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(SETTLE_MS))
        }
    }

    private companion object {
        /** One frame at 60Hz, matching `VeilFade.FRAME_MS`. */
        const val FRAME_MS = 16L

        /** The Stop pill's own short exit, and the LONGEST exit on the surface — read off the
         * production constants so a retune moves the samples with the code rather than reddening a
         * test that is still describing the old timing. `lower()` adds a frame of slack after each,
         * which is private to it, so every sample below is placed clear of the boundary. */
        val PILL_EXIT_MS = DEVICE_CONTROL_PILL_DISMISS_MS.toLong()
        val FULL_EXIT_MS = DEVICE_CONTROL_EXIT_MS.toLong()

        /**
         * Longer than any single wait on this surface: [DEVICE_CONTROL_EXIT_MS] — the LONGEST exit,
         * which is what `DeviceControlOverlay.lower` waits out before the last window comes down —
         * plus its frame of slack, and the veil's own ramp plus settle (`VeilFade`).
         *
         * **Derived from the exit and not from [DEVICE_CONTROL_DISMISS_MS].** The two are equal
         * today, so nothing here would go red on the difference; the point is that the exit is the
         * `maxOf` and the dismiss is only one of its two arguments, so a future surface whose pill
         * outlives its decoration would leave every test above idling for less than the code waits.
         * That failure arrives as an intermittent red, which is worse here than no test at all.
         */
        val SETTLE_MS = DEVICE_CONTROL_EXIT_MS + 500L
    }
}
