package com.mewbo.aura.data.device

import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

/** [AppForegroundChecker.requireForeground] backs the shared guard [SetAlarmHandler]/
 * [SetTimerHandler]/[DismissAlarmHandler] all call before `context.startActivity` (review finding
 * F4: a background launch is silently discarded by Android's API 29+ restriction, so proceeding
 * would report a false `handed_to_clock_app=true`). Tested once here against the seam directly -
 * none of those three handlers are constructible in a plain-JVM test (real `Context`). */
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
}
