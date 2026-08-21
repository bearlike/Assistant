package com.mewbo.aura.ui.control

/**
 * One step of the veil's choreography: how bright the overlay's windows are, whether they are on
 * screen at all, and how long that state is held before the next step.
 *
 * **[envelope] is a MULTIPLIER on each window's own resting opacity, never an absolute alpha.** The
 * decoration window rests at the Android 12 obscuring ceiling and the Stop pill rests at full; a
 * step writing an absolute value would have to know both, and getting the decoration one wrong is
 * not a cosmetic bug — restored to 1.0 that window silently swallows every touch on the screen, the
 * user's and the agent's injected taps alike, with nothing reporting a problem. A multiplier cannot
 * express that mistake.
 *
 * **[visible] is a second, non-redundant fact.** [envelope] is a compositor property
 * (`WindowManager.LayoutParams.alpha`) — it fades without the app redrawing anything, which is why
 * it is the fade mechanism — but a window at alpha 0 is still in the input dispatcher's list, so it
 * would still take the tap `device_action` is about to inject. Only [visible] takes a window out of
 * input. Collapsing the two looks like a simplification and re-opens a failure nothing reports.
 */
internal data class VeilStep(
    val envelope: Float,
    val visible: Boolean,
    val holdMs: Long,
)

/**
 * The order in which the device-control overlay leaves the screen for a capture or an injected
 * touch, and comes back afterwards.
 *
 * **A script rather than a hand-inlined sequence, because the ORDER is the correctness.** A capture
 * that catches a half-faded glow is worse than one that catches a lit one — it reads as a rendering
 * fault rather than as a deliberate surface — so the fade has to be FINISHED before the windows go,
 * and the windows have to be gone for [WINDOW_SETTLE_MS] before the capture reads the framebuffer.
 * Expressed as data, that sequence is asserted on a plain JVM with no Android and no Compose on the
 * classpath; inlined into the window controller it would only ever be assertable on a device.
 *
 * Nothing here reads a clock or a window — the durations arrive as constructor arguments, so a test
 * scripts a one-frame fade instead of sleeping through a real one.
 */
internal class VeilFade(
    private val fadeMs: Long = FADE_MS,
    private val frameMs: Long = FRAME_MS,
    private val windowSettleMs: Long = WINDOW_SETTLE_MS,
) {

    /**
     * Ease the windows off, then take them out of the frame and hold them there long enough for the
     * compositor to catch up. The LAST step is the state the capture — or the injected touch — runs
     * under, which is what makes "the fade is complete by then" a sequence rather than a race.
     */
    fun hide(): List<VeilStep> = buildList {
        val frames = frames()
        for (frame in 1..frames) {
            add(VeilStep(envelope = 1f - frame.toFloat() / frames, visible = true, holdMs = frameMs))
        }
        // Fully transparent AND out of input dispatch, held for the settle. Two different facts;
        // see [VeilStep.visible] for why neither one implies the other.
        add(VeilStep(envelope = 0f, visible = false, holdMs = windowSettleMs))
    }

    /**
     * Put the windows back transparent first so nothing pops, then ramp to exactly their resting
     * opacity.
     *
     * The final envelope is exactly `1f` rather than a summed float: a restore landing at 0.98
     * leaves an agent driving the phone behind a permanently dimmed announcement, and no later
     * event corrects it — every subsequent veil would ramp back to the same wrong value.
     */
    fun show(): List<VeilStep> = buildList {
        // Visible again while still fully transparent. The window manager needs a frame to bring
        // the surface back, and starting the ramp from zero is what keeps that frame invisible.
        add(VeilStep(envelope = 0f, visible = true, holdMs = frameMs))
        val frames = frames()
        for (frame in 1..frames) {
            add(VeilStep(envelope = frame.toFloat() / frames, visible = true, holdMs = frameMs))
        }
    }

    /**
     * At least one, so a zero-length or sub-frame fade still produces a script that ENDS in the
     * right state. An empty ramp would strand the windows wherever the previous step left them —
     * hidden, on the restore path.
     */
    private fun frames(): Int = ((fadeMs + frameMs - 1) / frameMs).toInt().coerceAtLeast(1)

    companion object {
        /**
         * How long the glow takes to ease off before the windows go, and to come back after.
         *
         * **Chosen SHORT because this runs on `tap`/`swipe`/`type`, not only on a screenshot.** Every
         * millisecond is paid on every action the agent injects, and a leisurely fade would make the
         * glow visibly pulse each time it touches the screen — a worse artefact than the snap this
         * replaces. Six frames at 60Hz is about the shortest ramp that reads as a fade rather than
         * as a cut; the house's own quick flat fade (`AuraMotion.reducedBlockFadeMs`) is 100ms, and
         * this is that value snapped to whole frames so the last step lands on a frame boundary
         * instead of a fraction of one.
         *
         * Deliberately NOT read from `AuraMotion`: this is window timing, next to
         * [WINDOW_SETTLE_MS], and a pure test of this file must not class-load a Compose object.
         */
        const val FADE_MS = 96L

        /**
         * One frame at 60Hz. Each step costs a `WindowManager.updateViewLayout` — a relayout, not a
         * free interpolation — so the step count is a real cost rather than free smoothness, and a
         * finer step than the compositor can show would buy nothing for it.
         */
        const val FRAME_MS = 16L

        /**
         * Long enough for a hidden window's redraw to reach the compositor before a capture reads
         * the framebuffer, and **still the one unproven number on this surface** — the platform
         * exposes no "the screen no longer contains this window" signal to wait on.
         *
         * Held AFTER the fade completes, so lengthening the fade can never eat into it.
         */
        const val WINDOW_SETTLE_MS = 48L
    }
}
