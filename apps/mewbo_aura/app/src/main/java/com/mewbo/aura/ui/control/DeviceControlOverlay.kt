package com.mewbo.aura.ui.control

import android.content.Context
import android.graphics.PixelFormat
import android.os.SystemClock
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import com.mewbo.aura.data.device.AppForegroundChecker
import com.mewbo.aura.data.device.DeviceControlSession
import com.mewbo.aura.data.device.ScreenCaptureVeil
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.di.ApplicationScope
import com.mewbo.aura.notify.RunNotificationService
import com.mewbo.aura.ui.theme.AuraTheme
import dagger.Lazy
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.SharingStarted
import com.mewbo.aura.data.device.DeviceShape

/**
 * The window that tells the user an agent is driving their phone — raised by the grant, and by
 * nothing else.
 *
 * **Why a `TYPE_APPLICATION_OVERLAY` window and not the assist overlay.** The existing overlay is a
 * `VoiceInteractionSession`: modal, and it would swallow the very taps `device_action` injects.
 * This surface has to sit over OTHER apps while staying out of their way, which is what the
 * platform's own overlay window type is for. No new dependency; the whole mechanism is
 * `WindowManager.addView`.
 *
 * **Two windows, because touch pass-through has exactly one supported shape.** A window receives
 * every touch inside its own bounds and there is no supported way for a full-screen one to be
 * selectively transparent to touch — an unhandled event is NOT forwarded to the window behind. So
 * the decoration (glow + narration) lives in a full-screen `FLAG_NOT_TOUCHABLE` window that can
 * never take a touch, and the Stop pill lives in a small `FLAG_NOT_TOUCH_MODAL` window whose
 * bounds ARE the touchable region.
 *
 * **`FLAG_SECURE` is not how the overlay stays out of a screenshot, and reaching for it is
 * destructive** — measured at shell UID, one secure window makes the ENTIRE capture fail rather
 * than hiding one layer. Suppression is temporal instead: this class IS the [ScreenCaptureVeil]
 * the capture path calls, taking the windows out of the frame for the duration and putting them
 * back however the capture ends.
 *
 * **`SYSTEM_ALERT_WINDOW` is a special permission, so every path here degrades silently.** No
 * grant, no window — device control keeps working exactly as it did, minus the announcement. This
 * surface must never be able to block or fail a grant.
 */
@Singleton
class DeviceControlOverlay @Inject constructor(
    @ApplicationContext private val context: Context,
    private val grant: DeviceControlSession,
    /**
     * **`Lazy`, and it is structural rather than an optimisation.** This class is the
     * [ScreenCaptureVeil], which `DeviceUiHandler` needs, which the executor needs, which
     * `RunRepository` needs — so an eager injection here closes a construction cycle Dagger
     * refuses to build (it also closes a second one, through `RunNotifications`). Deferring the
     * lookup to first use is honest as well as necessary: the repository is not touched until a
     * session is being narrated, which is strictly after everything here exists.
     */
    private val runs: Lazy<RunRepository>,
    private val settings: SettingsStore,
    /**
     * Whether the user can already see this session somewhere better than a bubble.
     *
     * The EXISTING seam, reused verbatim rather than given a second definition: it is process
     * importance OR the assist overlay being on screen, and both of those render the transcript —
     * `MainActivity`'s chat and the overlay's own `ChatTranscript`. So the one predicate answers
     * the question this surface actually has, which is not "is the app running" but "would a
     * bubble be repeating something already on screen".
     */
    private val foreground: AppForegroundChecker,
    /**
     * Injected rather than read from `LocalDeviceShape`: these windows are added straight to the
     * `WindowManager`, so nothing above them provides that local and reading it would silently
     * yield the `Handheld` default. Two answers come from it — how far the glow may rise, and
     * whether the bubbles stand down inside our own app.
     */
    private val shape: DeviceShape,
    @ApplicationScope private val scope: CoroutineScope,
) : ScreenCaptureVeil {

    private val windows = context.getSystemService(Context.WINDOW_SERVICE) as? WindowManager

    private val narration = MutableStateFlow(DeviceControlNarration())

    /** Drives every component's arrival and departure — the glow, the narration stack and the Stop
     * pill all read this one flow, so none of them can arrive or leave on its own schedule. Flipped
     * false BEFORE the windows come down so the surface eases off in place; removing a lit window
     * outright is the abrupt on->off flash the photosensitivity law forbids. */
    private val showing = MutableStateFlow(false)

    /**
     * Whether the narration bubbles have anything to add — true only while the user is somewhere
     * this session is NOT already legible.
     *
     * **The glow spans the grant; the bubbles do not.** In the app the transcript is already saying
     * what the agent is doing, in full, with history — a bubble stack repeating the last line over
     * the top of it is noise. Outside the app there is nothing else at all, which is the case the
     * whole surface exists for.
     *
     * Starts `false`: a grant is nearly always taken from a run the user just started in the app,
     * so the honest opening assumption is that they are looking at it.
     */
    private val outsideApp = MutableStateFlow(false)

    /**
     * Whether the bubbles have anything to add RIGHT NOW — [outsideApp], widened by the one shape
     * that reads the in-app transcript differently.
     *
     * On a handheld "the user is in the app" genuinely means "the transcript is already saying
     * this", so the stack stands down. On a television the same transcript is small text read from
     * across a room, on the very screen the agent is driving — so it answers the question wrongly
     * there, and the shape says so ([DeviceShape.narratesOverOwnApp]).
     *
     * Derived rather than folded into [outsideApp] itself, so that flow keeps meaning exactly what
     * its name says and the widening stays visible at the one place it applies.
     */
    private val narrating: StateFlow<Boolean> = outsideApp
        .map { it || shape.narratesOverOwnApp }
        .stateIn(scope, SharingStarted.Eagerly, shape.narratesOverOwnApp)

    /**
     * The session whose events narrate this overlay, or `null` if nobody has said.
     *
     * A grant is app-wide and carries no session of its own, so the driving session has to be
     * told to us — and it is told at RUN START, which is strictly before the grant exists (the
     * model calls `device_control_start` several steps into the run). Null is an ordinary state,
     * not an error: the glow and the Stop pill still raise, and only the narration is missing.
     */
    @Volatile private var drivingSessionId: String? = null

    /**
     * The two windows while they are up, in the order they were ADDED — which is the order the veil
     * walks them in and the reverse of the order they come down in.
     *
     * Written on the main thread only; read off it by [hiddenDuring] and [follow], which is why it
     * is volatile rather than plain. Non-empty means the windows are on screen — the fact both of
     * those actually need, and one [showing] cannot answer (it goes false while the dismiss fade is
     * still playing).
     */
    @Volatile private var veiledWindows: List<VeiledWindow> = emptyList()

    /** The fade/hide/restore choreography, as data. Defaults only; see [VeilFade]. */
    private val veil = VeilFade()

    private var owner: OverlayViewOwner? = null
    private var narrationJob: Job? = null

    /** Serialises [hiddenDuring]: two overlapping captures must not let the first one's restore
     * put the windows back while the second is still capturing. */
    private val captureLock = Mutex()

    init {
        // **A grant can end with nobody calling stop** — the Shizuku binder dies with its host
        // process and the grant demotes itself, which only a collector sees. The window is raised
        // and lowered by that flow alone, never by the call sites that take and release control,
        // so an overlay saying the phone can be driven cannot outlive the channel behind it.
        scope.launch(Dispatchers.Main.immediate) {
            grant.active.collect { held -> if (held) raise() else lower() }
        }
    }

    /**
     * Name the session whose narration this overlay draws.
     *
     * Called at run start, where the session id and "this run may drive the phone" are both
     * already known. Idempotent, and safe while a grant is already held: it restarts the
     * subscription on the new session.
     */
    fun follow(sessionId: String) {
        drivingSessionId = sessionId
        // Only while there is something to narrate ONTO. A run following a denied overlay
        // permission must not open a subscription nothing will ever draw.
        if (veiledWindows.isNotEmpty()) scope.launch(Dispatchers.Main.immediate) { startNarration() }
    }

    /**
     * Tear every window down NOW, and release the grant behind them — the escape hatch.
     *
     * **This exists because every other path here is cooperative, and the failure being escaped is
     * that one of them did not cooperate.** The windows are raised and lowered by the `grant.active`
     * collector alone, which is correct and is also the whole exposure: if the grant is never
     * released, or the collector is wedged, or `lower()` is cancelled mid-teardown, the announcement
     * stays on screen with nothing left that will take it away. On a handheld that is ugly. On a
     * television it is unrecoverable without force-stopping the app, because the Stop pill is not
     * even put up there ([DeviceShape.overlayCanHostControls]) and the notification's Stop action is
     * not practically reachable either.
     *
     * Three ways it deliberately differs from [lower]:
     *
     * - **It does not wait.** `lower()` holds for the full exit so the glow eases off rather than
     *   cutting, which is the photosensitivity law. That law protects a user watching a surface
     *   behave normally; someone reaching for this has a surface that is NOT behaving normally and
     *   wants it gone. A single monotonic cut is the lesser harm.
     * - **It tolerates an in-flight `lower()` rather than cancelling one.** A `lower()` suspended
     *   inside its own delay is a likely state to be in, and cancelling it would mean cancelling the
     *   collector that owns the whole raise/lower seam — after which no future grant could ever
     *   raise a window. So it is left to run: every window operation is `runCatching` and the state
     *   it writes is the same state this method already wrote, so it resumes into a no-op.
     * - **It releases the grant AFTER the windows are gone**, not before. Releasing first would ask
     *   the collector to lower windows this is about to remove, racing itself. `stop()` is
     *   documented idempotent, so the collector's own `lower()` arriving later is a no-op.
     *
     * Idempotent and safe with nothing on screen. Never throws: every window operation is
     * `runCatching`, because the one caller is someone already trying to escape a bad state.
     *
     * Returns whether there was anything to clear, read BEFORE the teardown is launched. The caller
     * is a person who just pressed a button and deserves to be told which of the two things
     * happened; a flat "cleared" for a screen that had no overlay is the guess this app's own
     * status rule forbids. It answers "was a window up", not "did the removal succeed" — the
     * removal is asynchronous and, being best-effort by design, has no failure worth reporting.
     */
    fun forceTeardown(): Boolean {
        val hadWindows = veiledWindows.isNotEmpty()
        scope.launch(Dispatchers.Main.immediate) {
            stopNarration()
            val wm = windows
            veiledWindows.forEach { runCatching { wm?.removeViewImmediate(it.view) } }
            veiledWindows = emptyList()
            owner?.destroy()
            owner = null
            showing.value = false
            narration.value = DeviceControlNarration()
            // Last, so the collector's lower() finds nothing to do rather than racing this.
            grant.stop()
        }
        return hadWindows
    }

    /**
     * Take the overlay out of the frame for the duration of [block], easing it off first and back
     * on afterwards.
     *
     * **The overlay composites into `screencap`** — a plain overlay window renders as a bright
     * band across the capture, and the model would then reason about our own chrome as if it were
     * the user's screen.
     *
     * **The fade is not decoration.** This runs on every injected tap, swipe and keystroke as well
     * as on a screenshot, so a hard hide is a strobe the user watches for as long as the agent
     * works. It is a WINDOW-alpha ramp rather than a Compose animation: alpha is a compositor
     * property, so it needs no app redraw and keeps working while a shell-UID capture has the
     * render thread busy. Order, durations and the reason the fade cannot bleed into the capture
     * live in [VeilFade].
     *
     * Free whenever nothing is drawn: the fast path returns before touching a window or a
     * dispatcher, which matters because a capture is already the expensive observation and most
     * captures happen with no overlay on screen at all.
     */
    override suspend fun <T> hiddenDuring(block: suspend () -> T): T {
        if (veiledWindows.isEmpty()) return block()
        return captureLock.withLock {
            try {
                // Inside the `try`, so a cancellation landing DURING the fade still restores. A
                // half-faded window left behind by a cancelled ramp is the same lie as a hidden
                // one: the phone is being driven and the announcement is not on screen.
                play(veil.hide())
                block()
            } finally {
                // NonCancellable: a cancelled capture must never strand the windows invisible —
                // the user would be left with an agent driving their phone and nothing saying so.
                // It holds a cancellation or a throw for the ramp's own length, which is the price
                // of never restoring with a snap — and the throw path (a FLAG_SECURE screen
                // refusing the capture) is precisely the one the user is looking at.
                withContext(NonCancellable) { play(veil.show()) }
            }
        }
    }

    /**
     * Walk a veil script, one step at a time.
     *
     * Hops to the main thread ONCE for the whole walk rather than per step: `delay` suspends rather
     * than blocking, so holding the main dispatcher across the ramp costs nothing and saves a dozen
     * context switches on a path the agent takes for every tap.
     */
    private suspend fun play(steps: List<VeilStep>) = withContext(Dispatchers.Main.immediate) {
        for (step in steps) {
            // Re-read per step rather than captured once: a grant ending mid-capture takes the
            // windows down under us, and the honest response is to stop touching them — not to
            // put back a surface whose grant is over.
            veiledWindows.forEach { it.apply(step) }
            delay(step.holdMs)
        }
    }

    private fun raise() {
        if (veiledWindows.isNotEmpty()) {
            showing.value = true
            startNarration()
            return
        }
        val wm = windows ?: return
        // The special permission's ONLY gate. Re-read on every raise rather than cached: the user
        // can grant it in Settings at any time, and an app that only ever asked once would stay
        // silent for the rest of its install.
        if (!Settings.canDrawOverlays(context)) return

        val viewOwner = OverlayViewOwner()
        val aura = composeView(viewOwner) {
            val state by narration.collectAsState()
            val visible by showing.collectAsState()
            val narrating by this@DeviceControlOverlay.narrating.collectAsState()
            DeviceControlAura(
                bubbles = state.bubbles,
                visible = visible,
                narrating = narrating,
                shape = shape,
            )
        }
        // **The pill goes up only where it can be pressed.** Both windows carry
        // `FLAG_NOT_FOCUSABLE`, which is what lets the agent's injected input reach the app
        // underneath — and a window with that flag receives no key events at all. A finger does not
        // need them; a D-pad has nothing else. So on a television this pill was a control drawn
        // above everything and pressable by nobody, which is worse than no control: it is the one
        // thing on screen claiming the grant can be ended here. `overlayCanHostControls` says
        // which shape that is, and its KDoc records why making the window focusable is not the
        // cure. The stop for that shape lives in the app, where the D-pad already reaches.
        val pill = if (shape.overlayCanHostControls) {
            composeView(viewOwner) {
                // The same flow the decoration reads, so the pill arrives with the glow rather than
                // being the one component that pops in. Its EXIT is the short one, and that lives in
                // the composable's own envelope.
                val visible by showing.collectAsState()
                DeviceControlStopPill(visible = visible, onStop = ::stop)
            }
        } else {
            null
        }
        // Held rather than passed inline: `updateViewLayout` needs the SAME params instance back,
        // and each one carries its window's resting alpha, which is what the veil scales.
        val auraLayout = auraParams()
        val pillLayout = pillParams()
        // addView still throws if the permission was revoked between the check above and here, or
        // if the display is gone. Degrade silently and leave the grant untouched.
        val added = runCatching {
            wm.addView(aura, auraLayout)
            // Added SECOND so it sits above the decoration: the Stop pill must stay reachable
            // even while the glow is at its brightest.
            if (pill != null) wm.addView(pill, pillLayout)
        }.isSuccess
        if (!added) {
            runCatching { wm.removeViewImmediate(aura) }
            viewOwner.destroy()
            return
        }
        owner = viewOwner
        veiledWindows = listOfNotNull(
            VeiledWindow(wm, aura, auraLayout),
            pill?.let { VeiledWindow(wm, it, pillLayout) },
        )
        showing.value = true
        // After the windows exist, so a subscription is only ever opened for a surface that can
        // actually draw it.
        startNarration()
    }

    private suspend fun lower() {
        stopNarration()
        val up = veiledWindows
        if (up.isEmpty()) return
        showing.value = false
        val wm = windows
        // Reverse of the add order — the pill sits above the decoration, so it comes off first —
        // and it comes off SOONER. Its prompt departure is what confirms the tap landed, and
        // REMOVING the window is what actually ends its touch region: a window faded to nothing is
        // still in the input dispatcher's list (see [VeilFade]), and this one sits bottom-centre
        // over the app the user is already reaching past. The extra frame is slack for the last
        // animation tick to reach the compositor.
        //
        // Named by ROLE rather than by position, because a shape that hosts no pill puts exactly
        // one window in this list: under `first()`/`last()` the decoration would then answer to
        // both, be torn out on the PILL's short timer, and lose the ease-off entirely — the abrupt
        // on->off luminance change the long exit exists to prevent, on the one shape watched from
        // across a dark room.
        val decoration = up.first().view
        val stopPill = up.getOrNull(1)?.view
        delay(DEVICE_CONTROL_PILL_DISMISS_MS.toLong() + FRAME_MARGIN_MS)
        if (stopPill != null) runCatching { wm?.removeViewImmediate(stopPill) }
        // Then the decoration, once the glow has finished easing off IN PLACE — removing a lit
        // surface is instantaneous, and that on->off luminance change is the flash the fade exists
        // to avoid. The remainder is DERIVED from the longest exit rather than restated, so
        // retuning either fade can never leave the teardown short of one still in flight.
        delay(DEVICE_CONTROL_EXIT_MS.toLong() - DEVICE_CONTROL_PILL_DISMISS_MS.toLong())
        runCatching { wm?.removeViewImmediate(decoration) }
        owner?.destroy()
        owner = null
        veiledWindows = emptyList()
        // A later grant starts with a clean surface rather than replaying the last one's lines.
        narration.value = DeviceControlNarration()
    }

    /**
     * The user's Stop, routed through the SAME intent the notification's Stop action fires.
     *
     * ONE way to end a grant, deliberately: the service releases the grant AND ends the watches
     * that hold the command channel open, and a second release path here would be a second
     * opinion about how long an agent may drive the phone. The start is legal from the
     * background because a held grant implies the service is already running in the foreground —
     * and if it somehow is not, the tap does nothing rather than crashing the overlay.
     */
    private fun stop() {
        runCatching { context.startService(RunNotificationService.stopIntent(context)) }
    }

    /**
     * Follow the driving session for as long as the overlay is up.
     *
     * A purely PASSIVE subscriber, the same shape as the notification watcher: device-tool
     * dispatch is a step in `live()`'s own pipeline, upstream of its multicast, so following a
     * run cannot double-answer a tool call. It does hold the shared SSE subscription open while
     * the overlay is showing, which is the behaviour the device-control hold wants anyway.
     */
    private fun startNarration() {
        val sessionId = drivingSessionId ?: return
        narrationJob?.cancel()
        narrationJob = scope.launch {
            launch {
                runs.get().live(sessionId).collect { event ->
                    narration.update { it.fold(event, SystemClock.uptimeMillis()) }
                }
            }
            // Expiry is a clock concern, so it lives out here rather than inside the fold. The
            // tick is what makes a line disappear when the run has simply gone quiet — without
            // it, the last thing said would stay on screen until the next event arrived.
            launch {
                while (isActive) {
                    // Read on the tick that ALREADY exists rather than on a second one, and by
                    // polling because a poll is all that is available: the app carries no
                    // process-lifecycle observer and `di/DeviceModule` deliberately declined the
                    // dependency that would provide one. Half a second of lag on a bubble stack
                    // whose lines live four seconds is not a fact anybody can perceive, and the
                    // binder read only happens while an agent is actively driving the phone.
                    outsideApp.value = !foreground.isForeground()
                    delay(EXPIRY_TICK_MS)
                    narration.update { it.expire(SystemClock.uptimeMillis()) }
                }
            }
        }
    }

    private fun stopNarration() {
        narrationJob?.cancel()
        narrationJob = null
    }

    private fun composeView(
        viewOwner: OverlayViewOwner,
        content: @Composable () -> Unit,
    ): ComposeView = ComposeView(context).apply {
        // Both windows share one owner, so give each view its own id: Compose keys its saved-state
        // provider off the view, and two id-less views under one SavedStateRegistry is a collision
        // waiting to be discovered on a device rather than here.
        id = View.generateViewId()
        setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnViewTreeLifecycleDestroyed)
        // A window added straight to the WindowManager has no Activity to inherit these from, so
        // Compose would fail to compose without them — the same thing AuraSession does for the
        // assist overlay's own ComposeView.
        setViewTreeLifecycleOwner(viewOwner)
        setViewTreeSavedStateRegistryOwner(viewOwner)
        setContent {
            // AuraTheme's reducedMotion is a DEFAULTED parameter, so a bare AuraTheme { } here
            // would compile and silently drop the in-app toggle for this whole surface — the
            // trap that bit the assist overlay for a release cycle.
            val reducedMotion by settings.reducedMotion.collectAsState(initial = false)
            AuraTheme(reducedMotion = reducedMotion) { content() }
        }
    }

    private fun auraParams() = WindowManager.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT,
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            // The load-bearing one. Without it this window eats every tap on the screen,
            // including the ones device_action injects, and the feature breaks itself.
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
            // The glow's peak sits at the TRUE bottom edge (its falloff is anchored there), so
            // the window has to reach past the system bars or the brightest part is clipped.
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
            // Views added directly to the WindowManager do NOT inherit the manifest's hardware
            // acceleration — it has to be asked for here, and AGSL (RuntimeShader) has no
            // software path, so without this the glow simply does not draw.
            WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
        PixelFormat.TRANSLUCENT,
    ).apply {
        alpha = MAX_OBSCURING_ALPHA
        // **The cutout is a SEPARATE attribute from FLAG_LAYOUT_NO_LIMITS, and confusing the two
        // is what clipped this window's top edge.** No-limits governs the system bars; the cutout
        // has its own mode with its own default, under which `WindowLayout.computeFrames`
        // intersects a fullscreen window's PARENT frame with the display's cutout-safe rect. The
        // no-limits branch runs afterwards and resets only the DISPLAY frame, so it cannot undo
        // that — the window is still measured against the clipped parent, and on a device whose
        // cutout safe inset is the status-bar strip the glow stops exactly where that strip
        // begins.
        //
        // **The platform's own edge-to-edge enforcement does not reach this window.** It is
        // applied by `PhoneWindow.generateLayout`, which runs only for an Activity's or a
        // Dialog's decor; a view added straight to the WindowManager never passes through it, so
        // targeting a recent SDK grants this one nothing. Same no-Activity-no-inheritance trap as
        // FLAG_HARDWARE_ACCELERATED above.
        //
        // ALWAYS rather than SHORT_EDGES: short-edges relaxes only the two short sides, so a
        // cutout on a long edge still clips the perimeter, and the surface has to read as an
        // unbroken border in both orientations. Safe because a cutout is a HOLE rather than extra
        // screen: what extends into it here is decoration nobody is asked to read, while the
        // narration stack and the Stop pill are both anchored at the bottom.
        layoutInDisplayCutoutMode =
            WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
    }

    private fun pillParams() = WindowManager.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            // Redundant with NOT_FOCUSABLE, which implies it, but stated because it is the flag
            // that actually describes the intent: touches OUTSIDE this window's bounds belong to
            // whatever is underneath. The bounds are the pill, so the pill is the only touchable
            // region on the whole screen.
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
            WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
        PixelFormat.TRANSLUCENT,
    ).apply {
        // Deliberately NOT laid out in screen, unlike the glow: letting the window manager keep
        // this one inside the content area is what stops the pill landing under the gesture bar
        // on a device whose insets never reach a no-limits window.
        gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
    }

    private companion object {
        /**
         * The window opacity ceiling for letting touches through, and it is not cosmetic.
         *
         * Android 12 blocks touches that pass through a window an app cannot be trusted with, and
         * `TYPE_APPLICATION_OVERLAY` is explicitly not trusted. A `FLAG_NOT_TOUCHABLE` window is
         * exempt only while the combined obscuring opacity stays at or under the system maximum,
         * 0.8 by default — at the default 1.0 this window would silently drop EVERY touch to the
         * app underneath, the user's and the agent's alike, with nothing reporting a problem.
         */
        const val MAX_OBSCURING_ALPHA = 0.8f

        /** One frame of slack after the dismiss fade, so the last animation tick has landed
         * before the surface is torn out from under it. */
        const val FRAME_MARGIN_MS = 32L

        /** How often expiry is re-evaluated. Fine enough that a line leaves when it is supposed
         * to, coarse enough to cost nothing while an agent works. */
        const val EXPIRY_TICK_MS = 500L
    }
}

/**
 * One overlay window as the veil sees it: something to hide, and its own resting opacity.
 *
 * **The resting alpha is read off the params the window was ADDED with, never restated.** The
 * decoration window rests at the obscuring ceiling and the Stop pill at the platform default of
 * full opacity; a veil that wrote absolute alphas would have to carry both numbers, and writing 1.0
 * back onto the decoration window is the silent catastrophe that ceiling exists to prevent —
 * scaling makes restoring a window BRIGHTER than it was added unrepresentable.
 */
private class VeiledWindow(
    private val windows: WindowManager,
    val view: View,
    private val layout: WindowManager.LayoutParams,
) {
    private val restAlpha = layout.alpha

    fun apply(step: VeilStep) {
        // Visibility, not just alpha: a window at alpha 0 is invisible to the eye and still in the
        // input dispatcher's list, so this is what stops the Stop pill taking the tap
        // `device_action` is about to inject. See [VeilStep].
        view.visibility = if (step.visible) View.VISIBLE else View.INVISIBLE
        layout.alpha = restAlpha * step.envelope
        // `updateViewLayout` throws once the view has been removed, which is exactly what a grant
        // ending mid-capture does. There is nothing to restore then — the surface is already gone.
        runCatching { windows.updateViewLayout(view, layout) }
    }
}

/**
 * The lifecycle and saved-state owners a `ComposeView` needs when there is no Activity above it.
 *
 * Shared by both windows on purpose: they are raised and lowered as ONE surface, so two
 * independent lifecycles would only be two ways to leave half of it on screen.
 */
private class OverlayViewOwner : LifecycleOwner, SavedStateRegistryOwner {
    private val registry = LifecycleRegistry(this)
    private val savedState = SavedStateRegistryController.create(this)

    override val lifecycle: Lifecycle get() = registry
    override val savedStateRegistry: SavedStateRegistry get() = savedState.savedStateRegistry

    init {
        savedState.performRestore(null)
        registry.currentState = Lifecycle.State.RESUMED
    }

    /** Drives the views' own `DisposeOnViewTreeLifecycleDestroyed` strategy, so removing the
     * windows also tears the compositions down instead of leaking them. */
    fun destroy() {
        registry.currentState = Lifecycle.State.DESTROYED
    }
}
