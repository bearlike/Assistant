package com.mewbo.aura.data.device

import android.app.Application
import android.content.Context
import android.content.Intent
import android.os.Vibrator
import android.os.VibratorManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertSame
import org.junit.Test
import org.junit.runner.RunWith
import org.mockito.Mockito
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * minSdk dropped 33 -> 30, and [android.os.VibratorManager] is API 31+. [VibratorResolver] is the
 * one place that branches on it; [WakeAlarmReceiver] used to reference `VibratorManager` directly,
 * with no try/catch, inside a [android.content.BroadcastReceiver] — a `NoClassDefFoundError` there
 * is not caught by anything upstream. This suite runs the same claims at both floor (30) and the
 * previous floor (33), because a config that passes at one and crashes at the other is exactly the
 * failure mode minSdk 30 introduced.
 *
 * Plain JVM cannot see this class of failure — it needs a real `android-all` jar resolving real
 * classes at a pinned `SDK_INT`, which is what makes this suite the exception to the plain-JVM
 * default (`app/src/test/java/com/mewbo/aura/CLAUDE.md`).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30, 33])
class VibratorResolverTest {

    /** Claim 1: resolution itself never throws, at either API level. */
    @Test
    fun `resolve does not throw`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()

        // No assertion beyond "returns" — Robolectric provides no VibratorManager service
        // registration, so the API 31+ branch legitimately resolves to null (see the API-33 test
        // below for what IS observable about which branch ran).
        VibratorResolver.resolve(context)
    }

    /**
     * Claim 2 — the real regression guard. The old `WakeAlarmReceiver` referenced
     * `VibratorManager` directly with no try/catch; at minSdk 30 that risked a
     * `NoClassDefFoundError` the moment the class was touched on a device without it. Pinned at
     * API 30 specifically, since that is the level the old code never ran on in production.
     */
    @Config(sdk = [30])
    @Test
    fun `WakeAlarmReceiver onReceive completes without throwing at API 30`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val intent = Intent().putExtra(WakeAlarmReceiver.EXTRA_REASON, "test")

        // No try/catch here on purpose — a throw fails the test, which is the point.
        WakeAlarmReceiver().onReceive(context, intent)
    }

    /**
     * Claim 3 — at API 33 the resolver goes through the [VibratorManager] path, not the deprecated
     * fallback, and genuinely discriminates the two: a mock [VibratorManager] is registered as the
     * `VIBRATOR_MANAGER_SERVICE`, and [VibratorResolver.resolve] must return exactly the [Vibrator]
     * it hands back — not a coincidental null shared by both branches (an earlier version of this
     * test only proved that; a resolver that took the WRONG branch would have passed it too).
     */
    @Config(sdk = [33])
    @Test
    fun `resolve returns the VibratorManager's vibrator at API 33`() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        val expected = Mockito.mock(Vibrator::class.java)
        val manager = Mockito.mock(VibratorManager::class.java)
        Mockito.`when`(manager.defaultVibrator).thenReturn(expected)
        shadowOf(context).setSystemService(Context.VIBRATOR_MANAGER_SERVICE, manager)

        val resolved = VibratorResolver.resolve(context)

        assertSame(
            "at API 33 the resolver must read VIBRATOR_MANAGER_SERVICE, not the deprecated key",
            expected,
            resolved,
        )
    }

    /**
     * The API-30 mirror of the test above — a mock [Vibrator] registered under the deprecated
     * `VIBRATOR_SERVICE` key, which [VibratorResolver] must return unchanged below the [S][
     * android.os.Build.VERSION_CODES.S] floor. Together the two tests pin BOTH branches by
     * identity, so a resolver that always took one branch (or swapped them) fails one of the two.
     */
    @Config(sdk = [30])
    @Test
    fun `resolve returns the legacy VIBRATOR_SERVICE vibrator at API 30`() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        val expected = Mockito.mock(Vibrator::class.java)
        shadowOf(context).setSystemService(Context.VIBRATOR_SERVICE, expected)

        val resolved = VibratorResolver.resolve(context)

        assertSame(
            "at API 30 the resolver must read the deprecated VIBRATOR_SERVICE key",
            expected,
            resolved,
        )
    }
}
