package com.mewbo.aura.data.device

/**
 * What KIND of device this is, and therefore which component compositions the app assembles.
 *
 * **This is the one place the two product shapes are named.** A television is not a large phone:
 * it has no touchscreen, its only input device is four arrows and a confirm key, it is watched from
 * across a room, and the system surfaces a handheld takes for granted — the "Display over other
 * apps" screen among them — may not exist on it at all. Those are not degrees of the same design;
 * they are different designs, and the app renders whichever one the device asks for.
 *
 * **Behaviour lives HERE as members, never as a `when (shape)` at each reader.** A boolean asked at
 * twenty call sites is twenty independent chances to get the answer wrong, and adding a third shape
 * would mean finding all twenty. Adding one here is a compile error at every arm that has not
 * answered the new question — which is the point. The single legitimate `when` is the one that
 * picks between two whole composable trees, where exhaustiveness is what makes the choice safe.
 *
 * Declared in `data/` rather than beside its Compose consumers for the reason
 * [TelevisionChecker] already records: `data/` may not import `ui/`, and both `data/settings/` and
 * `voice/` read these answers. A shape declared at the top layer could not be reached from below.
 */
sealed interface DeviceShape {

    /**
     * Whether focusing a text field should raise the soft keyboard.
     *
     * On a handheld, focus only ever arrives from a tap, so "focused" and "wants to type" are the
     * same event. On a remote, focus travels THROUGH a field on the way past it, so raising the
     * keyboard on focus puts a full-screen IME in front of a user who was only navigating — and
     * Back dismisses the keyboard rather than moving focus, so the remote oscillates and never
     * gets past the field. There, typing has to be asked for.
     */
    val opensKeyboardOnFocus: Boolean

    /**
     * Whether the system exposes a screen for granting "Display over other apps" by hand.
     *
     * `false` does not mean the permission is unavailable — it means the intent that would ask for
     * it resolves to nothing, so a row wired to it is a button that silently does nothing. That is
     * what makes an alternative provisioning route necessary rather than merely convenient.
     */
    val hasOverlayPermissionScreen: Boolean

    /**
     * Whether a reply should be read aloud even when the request was TYPED.
     *
     * On a handheld, speech is something the user asked for by speaking — a typed turn staying
     * silent is the whole reason [com.mewbo.aura.voice.InputModality] gates the read-aloud path, and
     * flipping that would make every phone start talking at a user who has only ever tapped.
     *
     * A television has no voice entry point AT ALL: the assistant role is unreachable there, so
     * every request on that shape is typed or D-pad driven and therefore silent under the handheld
     * rule. It is also watched from across a room, where the transcript is not where the user is
     * getting their information. So on that shape the modality gate answers the wrong question, and
     * the user's own read-aloud switch is the one that should decide.
     */
    val narratesTextTurns: Boolean

    /**
     * Whether the device-control bubbles should draw while the user is inside OUR OWN app.
     *
     * The bubbles exist to say what an agent is doing where nothing else can. On a handheld the
     * chat transcript is already saying it, in full and with history, so a stack repeating the last
     * line over the top of it is noise — the surface stands down.
     *
     * On a television the same transcript is small text read from across a room while the agent is
     * driving the very screen it is drawn on, so "already legible" does not hold. The bubbles are
     * the only glanceable account of what is happening, and they stay up.
     */
    val narratesOverOwnApp: Boolean

    /**
     * How far up the surface the device-control decoration may reach, as a fraction of its height,
     * or `0f` for "no limit" (the shader skips the window entirely).
     *
     * The device-control glow renders the BORDER profile: at that balance the side rails run the
     * full height of the surface by construction, which reads as a frame on a tall handheld and as
     * a wash over most of a short, wide 16:9 panel. The cure is a bound on the rise, not a retune of
     * the border — the border is what makes the surface legible at every edge.
     *
     * A fraction rather than a dp: the complaint is about the PROPORTION of the screen the surface
     * eats, and a dp would say something different on every panel.
     */
    val controlAuraRiseFraction: Float

    /**
     * Whether a control drawn in the device-control overlay can actually be OPERATED here.
     *
     * The overlay's windows are added with `FLAG_NOT_FOCUSABLE`, which is what lets the agent's own
     * injected input reach the app underneath instead of being swallowed by our announcement. A
     * window carrying that flag receives NO key events at all — so on a handheld the Stop pill is
     * pressed with a finger and the flag costs nothing, while on a television, where the D-pad is
     * the only input, the very same pill is outside the focus system entirely and can never be
     * pressed.
     *
     * **Dropping the flag is not the cure and must not be tried.** A focusable overlay takes key
     * input from the app below, which is exactly where the agent's injected `key` presses land — it
     * would break device control on the one shape it was meant to fix. Worse, it would make a
     * leaked overlay swallow every D-pad press, so a user who can currently navigate away and
     * force-stop the app could no longer reach the launcher at all.
     *
     * `false` therefore means "draw the announcement, but do not put up a control nobody can press"
     * — a dead affordance is worse than an absent one — and the stop lives somewhere the D-pad
     * genuinely reaches.
     */
    val overlayCanHostControls: Boolean

    /** A phone or tablet: touch-first, every system settings surface present. */
    data object Handheld : DeviceShape {
        override val opensKeyboardOnFocus: Boolean = true
        override val hasOverlayPermissionScreen: Boolean = true
        override val narratesTextTurns: Boolean = false
        override val narratesOverOwnApp: Boolean = false
        override val controlAuraRiseFraction: Float = 0f
        override val overlayCanHostControls: Boolean = true
    }

    /**
     * An Android TV, Google TV or Fire TV: D-pad only, watched from a distance.
     *
     * `hasOverlayPermissionScreen = false` is measured behaviour on Fire OS rather than a
     * precaution — the user could not reach the toggle at all, so the overlay that announces an
     * agent driving the device was permanently and silently inert.
     */
    data object Television : DeviceShape {
        override val opensKeyboardOnFocus: Boolean = false
        override val hasOverlayPermissionScreen: Boolean = false
        override val narratesTextTurns: Boolean = true
        override val narratesOverOwnApp: Boolean = true

        /**
         * A 60% cut, which is the reduction asked for rather than a number tuned against a capture.
         *
         * Marked as such deliberately: nothing here has been measured on a panel, and if it still
         * reads tall this is the one knob to move.
         */
        override val controlAuraRiseFraction: Float = 0.4f
        override val overlayCanHostControls: Boolean = false
    }

    companion object {
        /**
         * `O(1)` — one feature lookup, via the existing [TelevisionChecker] seam.
         *
         * Kept as a resolver over that predicate rather than a second platform read, so there is
         * still exactly one place that asks Android what it is running on.
         */
        fun of(checker: TelevisionChecker): DeviceShape =
            if (checker.isTelevision()) Television else Handheld
    }
}
