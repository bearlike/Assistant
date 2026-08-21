package com.mewbo.aura.ui.settings

import android.content.Context
import android.provider.Settings
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Reads whether Mewbo may draw over other apps — the permission the device-control overlay needs
 * to say an agent is driving this phone.
 *
 * **`SYSTEM_ALERT_WINDOW` is a SPECIAL permission, so it is not a
 * [com.mewbo.aura.data.device.DevicePermissionChecker] question.** That seam asks
 * `checkSelfPermission`, which reports on the manifest declaration; this permission is additionally
 * gated by an app-op the user flips in system Settings, and only [Settings.canDrawOverlays]
 * consults it. Routing this through the runtime-permission checker would report granted for an app
 * whose window the window manager will refuse — the exact wrong-green this screen forbids.
 *
 * The read is synchronous, exact, and cheap, which is what lets the row re-read it on every resume
 * rather than guess. It is revocable from outside the app at any time, so a cached answer would go
 * stale silently; nothing here caches.
 *
 * Constructor-injected, so Hilt supplies it with no module of its own — same shape as
 * [AssistantRoleReader], for the same reason: the ViewModel stays free of a static platform call it
 * could not be tested around.
 */
@Singleton
class OverlayPermissionReader @Inject constructor(@ApplicationContext private val context: Context) {

    /** O(1) — one app-op lookup. Safe to call on every resume. */
    fun isGranted(): Boolean = Settings.canDrawOverlays(context)
}
