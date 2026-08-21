package com.mewbo.aura.data.device.shizuku

/**
 * Seam over "may this app draw over other apps", declared DOWN so this package
 * never imports the `ui/settings/` reader that answers it — the same
 * declared-down shape as
 * [com.mewbo.aura.data.device.ScreenCaptureVeil], bound in `di/DeviceModule`.
 *
 * It is deliberately the SAME read the settings row and the overlay's own
 * `raise()` use ([android.provider.Settings.canDrawOverlays]). A second way to
 * ask would let this class report a grant the window manager still refuses.
 */
fun interface OverlayPermissionState {
    fun isGranted(): Boolean
}

/**
 * What [ShizukuOverlayGrant.grant] did — never a bare boolean, for the reason
 * [com.mewbo.aura.data.device.DeviceControlGrant]'s KDoc spells out: "it did not
 * work" has several causes here and each needs a different action from the
 * person holding the device. A boolean has nowhere to put the reason.
 *
 * [message] is written for that person, so a caller can render it verbatim.
 *
 * It is the answer for BOTH routes in
 * [com.mewbo.aura.data.device.OverlayProvisioning], not only the app-op one — one union is what
 * lets the surface render a result without asking which route produced it.
 */
sealed interface OverlayGrantOutcome {
    /** snake_case discriminator, matching the vocabulary the grant union uses. */
    val code: String

    /** Whether the overlay permission reads granted BY this call. Never a guess: the hand-off arm
     * says `false` because it granted nothing, not because the permission is absent — the row's
     * own badge reads the live permission and is the only thing that claims that. Read it as "this
     * call granted it", never as "the app may draw". */
    val granted: Boolean

    /**
     * What to tell the person, always — refusals included, because a tap that does nothing and
     * says nothing is indistinguishable from a broken button.
     *
     * Non-null even for the hand-off arm. A nullable message would make "say nothing" the easy
     * default for the next arm somebody adds, and silence is the failure mode this whole surface
     * exists to remove; a hand-off has something worth saying anyway, since the screen it opens
     * lands on top of the one the user tapped.
     */
    val message: String

    /**
     * The user was sent to the system screen; nothing was granted here and nothing reports back.
     *
     * A special permission has no result callback, so this arm is honest about handing off rather
     * than claiming an outcome it cannot observe — the resume re-read of `canDrawOverlays` is what
     * eventually answers.
     */
    data object SentToSystemSettings : OverlayGrantOutcome {
        override val code = "sent_to_system_settings"
        override val granted = false
        override val message =
            "Opened the system screen. Turn on \"Display over other apps\" for Aura, then come back."
    }

    /** Nothing to do — the permission was already there. No command is run. */
    data object AlreadyGranted : OverlayGrantOutcome {
        override val code = "already_granted"
        override val granted = true
        override val message = "Display over other apps was already allowed."
    }

    /** The app-op write landed and the permission now reads granted. */
    data object Granted : OverlayGrantOutcome {
        override val code = "granted"
        override val granted = true
        override val message =
            "Allowed Aura to display over other apps. The on-screen sign that an agent is " +
                "driving this device will now appear."
    }

    /**
     * Shizuku cannot run the command, and [status] says which of the four
     * states it is in — the remedy differs for every one of them, so it is
     * carried rather than flattened into prose the caller cannot branch on.
     *
     * [DeviceControlStatus.Ready] is never carried here by the ready CHECK; it
     * appears only for a bind that failed against a ready status, which
     * [ShizukuOverlayGrant] reports as [DeviceControlStatus.NotRunning] for the
     * same reason [com.mewbo.aura.data.device.DeviceControlSession.start] does:
     * the user's remedy is identical to a down service, so inventing a fifth
     * state buys nobody a different action.
     */
    data class ShizukuUnavailable(val status: DeviceControlStatus) : OverlayGrantOutcome {
        override val code = "shizuku_unavailable"
        override val granted = false
        override val message = when (status) {
            DeviceControlStatus.NotInstalled ->
                "This needs Shizuku, which is not installed. Install Shizuku and start it, " +
                    "then try again."
            DeviceControlStatus.NotRunning ->
                "Shizuku's service is not running — normal after a restart. Open Shizuku, " +
                    "start it, then try again."
            DeviceControlStatus.PermissionDenied ->
                "Shizuku is running but has not authorised Aura. Authorise Aura in Shizuku, " +
                    "then try again."
            // Unreachable through grant(); kept so the `when` stays exhaustive
            // and a future caller constructing this arm still gets a sentence.
            DeviceControlStatus.Ready ->
                "Shizuku could not run the command. Open Shizuku, confirm it is running, " +
                    "then try again."
        }
    }

    /**
     * The command ran and the permission STILL reads false — its own outcome,
     * never folded into success.
     *
     * This is the house rule that a failed `screencap` still creates a file: an
     * exit code is not the check, so neither is a command that produced no
     * error text. [output] is the combined stdout+stderr, kept because it is the
     * only place the real cause is ever stated.
     */
    data class StillDenied(val output: String?) : OverlayGrantOutcome {
        override val code = "still_denied"
        override val granted = false
        override val message =
            "Shizuku ran the command but this device still refuses to let Aura display over " +
                "other apps."
    }
}

/**
 * Turns on "Display over other apps" for this app by writing its app-op through
 * the shell-UID channel device control already owns.
 *
 * **Why this exists at all: on some devices the system screen cannot be
 * reached.** `SYSTEM_ALERT_WINDOW` is a SPECIAL permission with no runtime
 * dialog, so the only ordinary route is
 * `Settings.ACTION_MANAGE_OVERLAY_PERMISSION`. On a television — Fire OS in
 * particular — that screen is not exposed, and the app-details fallback has no
 * toggle on it either, so the permission is ungrantable by hand and the
 * device-control overlay is permanently inert. Since it degrades silently by
 * design, nothing anywhere reports that.
 *
 * **`appops`, not `pm grant`, and the difference is not cosmetic.**
 * `Settings.canDrawOverlays` notes the `SYSTEM_ALERT_WINDOW` **app-op** first
 * and consults the manifest permission only when that op is still at its
 * default. So an op explicitly set to `deny` — which is what the system toggle
 * writes when the user turns it off — short-circuits before the permission is
 * ever read, and `pm grant` cannot move it. Measured at shell UID: `pm grant`
 * exits 0, prints nothing, and leaves the op exactly where it was, while
 * `appops set … allow` moves it deterministically.
 *
 * **This does not change the overlay's gate.** `canDrawOverlays` remains the
 * one thing `DeviceControlOverlay.raise()` asks; this makes it become true. The
 * system intent stays the primary route wherever it works — this is the
 * fallback for where it does not, and it is deliberately NOT gated on the
 * device being a television: a screen you cannot reach is the same problem on a
 * kiosk build or a stripped AOSP handheld, and the user must already have
 * installed and authorised Shizuku for it to do anything at all.
 *
 * Every collaborator is a narrow seam for the reason the rest of this package
 * uses them: the whole decision is exercised on a plain JVM with no Shizuku
 * binder, no Android framework and no `Context`.
 */
class ShizukuOverlayGrant(
    private val packageName: String,
    private val statusSource: DeviceControlStatusSource,
    private val shell: DeviceShellRunner,
    private val overlayState: OverlayPermissionState,
) {
    /**
     * Grant the overlay app-op, reporting what actually happened.
     *
     * `O(1)` — at most one shell round trip, and none at all when the
     * permission is already there.
     *
     * **Verified by EFFECT, never by exit code.** A zero exit from `appops`
     * says the command parsed, not that the window manager will now allow a
     * window; the answer is a second [OverlayPermissionState] read afterwards,
     * and a write that did not take is [OverlayGrantOutcome.StillDenied] rather
     * than a success.
     */
    suspend fun grant(): OverlayGrantOutcome {
        if (overlayState.isGranted()) return OverlayGrantOutcome.AlreadyGranted

        val status = statusSource.status().value
        if (!status.isReady) return OverlayGrantOutcome.ShizukuUnavailable(status)

        // `null` is a bind that did not happen — the status said Shizuku WOULD
        // allow one, which is not the same fact. Same remedy as a down service.
        val output = shell.run(command(), TIMEOUT_MS)
            ?: return OverlayGrantOutcome.ShizukuUnavailable(DeviceControlStatus.NotRunning)

        return if (overlayState.isGranted()) {
            OverlayGrantOutcome.Granted
        } else {
            OverlayGrantOutcome.StillDenied(output)
        }
    }

    /**
     * `cmd appops` rather than the bare `appops` wrapper: both work on AOSP, but
     * the wrapper is a shell script in `/system/bin` that an OEM image is free
     * not to ship, while `cmd` reaches the framework service directly. A package
     * name cannot contain a shell metacharacter, so it needs no quoting.
     */
    private fun command(): String = "cmd appops set $packageName $APP_OP allow"

    private companion object {
        const val APP_OP = "SYSTEM_ALERT_WINDOW"

        /** One `cmd` round trip. Generous enough for a cold AppOpsService,
         * far inside the service's own dispatch budget. */
        const val TIMEOUT_MS = 5_000
    }
}
