package com.mewbo.aura.ui.settings

import android.content.Context
import androidx.core.app.NotificationManagerCompat
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Reads whether Mewbo's notifications are enabled — the per-app toggle in system Settings.
 *
 * **Not a [com.mewbo.aura.data.device.DevicePermissionChecker] question, on ANY API level.**
 * `POST_NOTIFICATIONS` only became a runtime permission at API 33; below that,
 * `checkSelfPermission` reports `PERMISSION_GRANTED` unconditionally, because there is no
 * runtime permission to deny — so it reports "granted" for a user who switched notifications
 * off for the app in system Settings. [NotificationManagerCompat.areNotificationsEnabled]
 * delegates (API 24+, so on every API level this app targets) to the platform
 * `NotificationManager.areNotificationsEnabled()`, which reads the actual per-app toggle
 * directly — the fact this row needs below API 33, and still the right fact above it: per the
 * official runtime-permission guide (developer.android.com, "Notification runtime permission"),
 * denying `POST_NOTIFICATIONS` on API 33+ is defined to behave "similar to ... the user manually
 * [turning] off all notifications for your app in system settings," which this same call
 * already reports. Verified against the AndroidX source and that doc, not device-measured — no
 * API 33 device was in hand for this change.
 *
 * The read is synchronous, exact, and cheap, which is what lets the row re-read it on every
 * resume rather than guess. It is revocable from outside the app at any time, so a cached answer
 * would go stale silently; nothing here caches.
 *
 * Constructor-injected, so Hilt supplies it with no module of its own — same shape as
 * [OverlayPermissionReader]/[AssistantRoleReader], for the same reason: the ViewModel stays free
 * of a static platform call it could not be tested around.
 */
@Singleton
class NotificationPermissionReader @Inject constructor(@ApplicationContext private val context: Context) {

    /** O(1) — one binder round trip. Safe to call on every resume. */
    fun isGranted(): Boolean = NotificationManagerCompat.from(context).areNotificationsEnabled()
}
