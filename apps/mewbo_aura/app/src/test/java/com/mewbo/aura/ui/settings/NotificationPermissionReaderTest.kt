package com.mewbo.aura.ui.settings

import android.app.NotificationManager
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * [NotificationPermissionReader] at API 30 — below the `POST_NOTIFICATIONS` runtime permission
 * (API 33). The old implementation read `checkSelfPermission(POST_NOTIFICATIONS)`, which reports
 * `PERMISSION_GRANTED` unconditionally below API 33 — including for a user who switched
 * notifications off for the app in system Settings. This asserts the reader tells the truth in
 * both directions via `NotificationManagerCompat.areNotificationsEnabled()`, which delegates to
 * the platform per-app toggle on every API level this app targets (see the class KDoc).
 *
 * **Mutation-proven — and the OBSERVED result is not the one you would predict, so it is recorded
 * here rather than reasoned about again.** Swapping `isGranted()` back to
 * `checkSelfPermission(POST_NOTIFICATIONS) == PERMISSION_GRANTED` and re-running
 * (`--rerun-tasks`) fails **`reports true when notifications are enabled`** — NOT the disabled case.
 *
 * Why: Robolectric does not model the real-device quirk this reader exists for. On a real API-30
 * device `checkSelfPermission(POST_NOTIFICATIONS)` returns `GRANTED` unconditionally, so the
 * DISABLED case is the one that lies. Under Robolectric the permission is simply ungranted by
 * default, so the old expression returns `false` for both cases and it is the ENABLED case that
 * breaks. Same conclusion — the old expression cannot track the notification toggle, so the test
 * discriminates old from new — but by the opposite arm, and only because the harness diverges from
 * the device. **Do not "correct" this comment back to the intuitive version; it was measured.**
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30])
class NotificationPermissionReaderTest {

    private val context = RuntimeEnvironment.getApplication()
    private val reader = NotificationPermissionReader(context)

    private fun shadowNotificationManager() =
        shadowOf(context.getSystemService(NotificationManager::class.java))

    @Test
    fun `reports false when notifications are disabled at API 30`() {
        shadowNotificationManager().setNotificationsEnabled(false)

        assertFalse(reader.isGranted())
    }

    @Test
    fun `reports true when notifications are enabled at API 30`() {
        shadowNotificationManager().setNotificationsEnabled(true)

        assertTrue(reader.isGranted())
    }
}
