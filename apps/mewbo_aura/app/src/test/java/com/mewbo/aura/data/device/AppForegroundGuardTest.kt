package com.mewbo.aura.data.device

import android.app.ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
import android.app.ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND_SERVICE
import android.app.ActivityManager.RunningAppProcessInfo.IMPORTANCE_VISIBLE
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** [AppForegroundChecker.requireForeground] backs the shared guard [SetAlarmHandler]/
 * [SetTimerHandler]/[DismissAlarmHandler] all call before `context.startActivity` (review finding
 * F4: a background launch is silently discarded by Android's API 29+ restriction, so proceeding
 * would report a false `handed_to_clock_app=true`), and [canStartActivityNow] is the decision
 * `di/DeviceModule` composes that checker out of. Both are tested against the seams directly -
 * none of those three handlers, nor the `ActivityManager` read itself, are constructible in a
 * plain-JVM test (real `Context`). */
class AppForegroundGuardTest {

    @Test
    fun `requireForeground does nothing when the checker reports foreground`() {
        AppForegroundChecker { true }.requireForeground() // must not throw
    }

    @Test
    fun `requireForeground throws a DeviceToolError coded app_not_foreground when backgrounded`() {
        try {
            AppForegroundChecker { false }.requireForeground()
            fail("expected AppNotForegroundException")
        } catch (e: AppNotForegroundException) {
            assertEquals("app_not_foreground", e.code)
        }
    }

    @Test
    fun `a foreground process importance alone permits an activity launch`() {
        assertTrue(canStartActivityNow(processImportance = IMPORTANCE_FOREGROUND, assistOverlayVisible = false))
    }

    @Test
    fun `a visible assist overlay alone permits an activity launch, whatever the process importance says`() {
        // THE clock/alarm-from-the-overlay case, and the reason it was broken. Showing a
        // VoiceInteractionSession rebinds the host service with
        // BIND_TREAT_LIKE_VISIBLE_FOREGROUND_SERVICE, so the process reports
        // IMPORTANCE_FOREGROUND_SERVICE (125) - NOT IMPORTANCE_FOREGROUND (100). Importance counts
        // UP as it gets less important, so the old `<= 100` check read "backgrounded" and
        // device_set_alarm/set_timer/dismiss_alarm all failed app_not_foreground, even though
        // Android itself permits the launch from a visible TYPE_VOICE_INTERACTION window. 125 is the
        // real case (AOSP-confirmed); IMPORTANCE_VISIBLE covers the same gap defensively should the
        // platform's bind flags ever change. Full chain in canStartActivityNow's KDoc.
        assertTrue(canStartActivityNow(processImportance = IMPORTANCE_FOREGROUND_SERVICE, assistOverlayVisible = true))
        assertTrue(canStartActivityNow(processImportance = IMPORTANCE_VISIBLE, assistOverlayVisible = true))
    }

    @Test
    fun `neither a foreground process nor a visible overlay forbids the launch`() {
        assertFalse(canStartActivityNow(processImportance = IMPORTANCE_VISIBLE, assistOverlayVisible = false))
        // Deliberately still FALSE, and this is the guard rail on the tempting wrong fix: relaxing
        // the importance THRESHOLD to <= IMPORTANCE_FOREGROUND_SERVICE (125) would also "fix" the
        // overlay, but it would hand the same permission to any plain foreground-service process
        // with no visible window at all - where the launch really is discarded. The overlay earns it
        // by having a visible window, so the overlay flag is what must carry it, not the threshold.
        assertFalse(canStartActivityNow(processImportance = IMPORTANCE_FOREGROUND_SERVICE, assistOverlayVisible = false))
        // A process absent from runningAppProcesses is "no evidence of foreground", never foreground.
        assertFalse(canStartActivityNow(processImportance = null, assistOverlayVisible = false))
    }
}
