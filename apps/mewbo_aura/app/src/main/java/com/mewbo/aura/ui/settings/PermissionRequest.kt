package com.mewbo.aura.ui.settings

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings

/**
 * Asks for a runtime permission, and falls through to the app's settings page
 * when Android will no longer show the dialog.
 *
 * **The fall-through is the whole point.** Once a permission has been denied
 * permanently — twice, or once with "don't ask again" — `requestPermissions`
 * returns immediately having shown nothing. A row wired straight to a launcher
 * therefore does nothing at all when tapped, with no dialog and no explanation,
 * and the user has no way to reach the grant from inside the app. That is
 * indistinguishable from a broken button.
 *
 * `shouldShowRequestPermissionRationale` is what tells the two apart, and it is
 * only meaningful AFTER a denial: it is `false` both before the first ask and
 * after a permanent one. So "have we asked before" has to be remembered, which
 * is why this takes an [asked] flag rather than deriving everything from the
 * platform.
 */
class PermissionRequest(
    private val context: Context,
    private val activity: Activity?,
) {
    /** True when every one of [permissions] is already granted. */
    fun allGranted(vararg permissions: String): Boolean =
        permissions.all {
            context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
        }

    /**
     * Whether the system dialog can still appear for [permissions].
     *
     * `true` before the first ask (nothing has been denied yet) and after an
     * ordinary denial (the OS will ask again). `false` only once the denial is
     * permanent, which is the case that needs the settings page instead.
     */
    fun canPrompt(asked: Boolean, vararg permissions: String): Boolean {
        val host = activity ?: return !asked
        if (!asked) return true
        return permissions.any { host.shouldShowRequestPermissionRationale(it) }
    }

    /**
     * Open this app's own settings page, where a permanently-denied permission
     * can still be granted by hand.
     *
     * Best effort: a device with no settings activity to resolve must not crash
     * the screen the user was on.
     */
    fun openAppSettings() {
        runCatching {
            context.startActivity(
                Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", context.packageName, null),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }

    /**
     * Open the system's "Display over other apps" screen for this app.
     *
     * **There is no dialog for this one.** `SYSTEM_ALERT_WINDOW` is a special
     * permission, so it cannot be requested through
     * [android.app.Activity.requestPermissions] at all — the only route is this
     * deep link into system Settings, which is also why the caller has no result
     * callback to refresh from and re-reads on resume instead.
     *
     * The package URI preselects Mewbo's own row. A device that cannot resolve
     * that form falls through to the app's own settings page, where the toggle
     * is still reachable — a tap that silently does nothing would read as a
     * broken button, which is the one outcome this screen never ships.
     */
    fun openOverlaySettings() {
        val scoped = Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (runCatching { context.startActivity(scoped) }.isSuccess) return
        openAppSettings()
    }

    /**
     * Open the Shizuku app, so the user can start its service or grant access
     * there. Falls back to its store page when it is not installed.
     */
    fun openShizuku() {
        val launch = context.packageManager.getLaunchIntentForPackage(SHIZUKU_PACKAGE)
        if (launch != null) {
            runCatching { context.startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
            return
        }
        runCatching {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(SHIZUKU_SITE))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }

    private companion object {
        const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"

        /** The project's own page rather than a store link: Shizuku is
         * distributed through more than one channel, and this one works
         * on a device with no store at all. */
        const val SHIZUKU_SITE = "https://shizuku.rikka.app/"
    }
}
