package com.mewbo.aura.ui.sessions

import com.mewbo.aura.data.model.SessionSummary
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** [RecentsFilter.matches] contract: ALL is unconditional, MOBILE_ONLY is a case-insensitive,
 * null-safe compare against [MOBILE_ORIGIN]. */
class RecentsFilterTest {

    private fun session(origin: String?) = SessionSummary(
        sessionId = "s",
        title = "Session",
        status = "idle",
        running = false,
        doneReason = null,
        origin = origin,
        recoverable = true,
        createdAt = "2026-01-01T00:00:00+00:00",
        updatedAt = "2026-01-01T00:00:00+00:00",
    )

    @Test
    fun `ALL matches every origin including null`() {
        assertTrue(RecentsFilter.ALL.matches(session("mobile")))
        assertTrue(RecentsFilter.ALL.matches(session("user")))
        assertTrue(RecentsFilter.ALL.matches(session(null)))
    }

    @Test
    fun `MOBILE_ONLY matches an exact mobile origin`() {
        assertTrue(RecentsFilter.MOBILE_ONLY.matches(session(MOBILE_ORIGIN)))
    }

    @Test
    fun `MOBILE_ONLY matches mobile origin regardless of case`() {
        assertTrue(RecentsFilter.MOBILE_ONLY.matches(session("Mobile")))
        assertTrue(RecentsFilter.MOBILE_ONLY.matches(session("MOBILE")))
    }

    @Test
    fun `MOBILE_ONLY rejects a non-mobile origin`() {
        assertFalse(RecentsFilter.MOBILE_ONLY.matches(session("user")))
    }

    @Test
    fun `MOBILE_ONLY rejects a null origin`() {
        assertFalse(RecentsFilter.MOBILE_ONLY.matches(session(null)))
    }
}
