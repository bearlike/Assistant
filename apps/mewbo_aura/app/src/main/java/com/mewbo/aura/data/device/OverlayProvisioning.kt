package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.OverlayGrantOutcome

/**
 * The two routes that exist for turning on "Display over other apps", each owning what it does.
 *
 * **Two STRATEGIES chosen by device shape, not one path with a rescue clause.** A touch device has
 * the system screen and every control surface a special permission needs; a television may not have
 * them at all ([DeviceShape.hasOverlayPermissionScreen] records what `false` costs). Those are
 * different provisioning designs, so they are members of a union rather than a branch at the row
 * that renders them — a `if (hasScreen)` at the call site is one more place to get wrong the moment
 * a third shape or a third route arrives, and it puts product behaviour in a Composable.
 *
 * **No I/O is imported here.** Both routes arrive as [Routes], a method ARG, which is what lets the
 * whole selection be exercised on a plain JVM with no Activity, no `Context` and no Shizuku binder.
 */
sealed interface OverlayProvisioning {

    /** The row's purpose caption — the CONSEQUENCE of the permission, plus how this device gets
     * it when that is not the ordinary way. */
    val caption: String

    /** What a control offering this route says it will do. Read by a SECONDARY row; the primary
     * row is labelled after the permission, not after the mechanism. */
    val actionLabel: String

    /**
     * The other route, offered alongside this one when it is not the primary.
     *
     * **Primary and available are different questions, deliberately.** Shape decides which route a
     * device leads with; it does not decide which routes exist. A handheld whose settings screen is
     * unreachable for its own reasons — a kiosk build, a stripped image, an OEM that hid the
     * screen — is the same problem a television has, and a user who simply prefers the one-tap
     * grant should not be told to go and find a screen. So the app-op route stays offered on a
     * handheld as a second control, gated only on Shizuku actually being ready.
     *
     * `null` on [ShizukuAppOp] because the reverse is not true: where the system screen does not
     * exist, offering it is offering a button that does nothing.
     */
    val alternative: OverlayProvisioning?

    /**
     * Take this route, and report what happened.
     *
     * `O(1)` — one intent, or one shell round trip. Both arms answer in the SAME
     * [OverlayGrantOutcome] union, which is what lets a caller render the result with no branch on
     * which route it took.
     */
    suspend fun provision(routes: Routes): OverlayGrantOutcome

    /**
     * The two I/O legs, injected per call.
     *
     * One object rather than per-arm parameters, because the arms must stay callable through the
     * interface — a signature that differed per member would put the `when` back at the call site
     * that this union exists to remove.
     */
    interface Routes {
        /** Deep-link into the system's "Display over other apps" screen. Returns nothing: a
         * special permission has no result callback, so the grant is learned on resume. */
        fun openSystemOverlayScreen()

        /** Write the app-op through the shell-UID channel device control already owns. */
        suspend fun grantThroughShizuku(): OverlayGrantOutcome
    }

    /**
     * Hand off to the system screen — the ordinary route, and the one a touch device leads with.
     *
     * There is nothing to verify here and nothing is claimed: the intent leaves the app, no result
     * comes back, and the row's truth arrives from the resume re-read of `canDrawOverlays`.
     */
    data object SystemSettingsScreen : OverlayProvisioning {
        override val caption = "Shows on-screen when an agent is driving your phone"
        override val actionLabel = "Open system settings"
        override val alternative: OverlayProvisioning? = ShizukuAppOp

        override suspend fun provision(routes: Routes): OverlayGrantOutcome {
            routes.openSystemOverlayScreen()
            return OverlayGrantOutcome.SentToSystemSettings
        }
    }

    /**
     * Write the app-op ourselves through Shizuku — the route for a device with no such screen.
     *
     * The mechanism, and why an app-op rather than a permission grant, is on
     * [com.mewbo.aura.data.device.shizuku.ShizukuOverlayGrant]. What matters here is only that it
     * answers in the same union, including a refusal that names what would fix it, because a route
     * that can fail silently is the one this whole seam exists to replace.
     */
    data object ShizukuAppOp : OverlayProvisioning {
        override val caption =
            "Shows on-screen when an agent is driving this device. Granted through Shizuku — " +
                "this device has no system screen for it."
        override val actionLabel = "Grant through Shizuku"
        override val alternative: OverlayProvisioning? = null

        override suspend fun provision(routes: Routes): OverlayGrantOutcome =
            routes.grantThroughShizuku()
    }

    companion object {
        /**
         * Which route this device leads with. `O(1)`, pure, and the ONE place the mapping is
         * spelled — a second reader deriving it from the shape again is how the row and whatever
         * counts it in a section header come to disagree.
         */
        fun primaryFor(shape: DeviceShape): OverlayProvisioning =
            if (shape.hasOverlayPermissionScreen) SystemSettingsScreen else ShizukuAppOp
    }
}
