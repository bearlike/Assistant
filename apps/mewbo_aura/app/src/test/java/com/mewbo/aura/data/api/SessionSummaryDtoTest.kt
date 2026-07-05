package com.mewbo.aura.data.api

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression test for a live-E2E bug (task-B review, fix round 2): the DEPLOYED Mewbo API never
 * sends `updated_at` on any session summary - only `session_id`, `title`, `created_at`, `status`,
 * and `done_reason` - even though the source api-contract.md was verified against always emits it.
 * `SessionSummaryDto` must tolerate every field but the true identity (`session_id`) being absent.
 */
class SessionSummaryDtoTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `parses the real minimal shape the deployed API actually sends`() {
        val raw = """
            {"sessions":[
                {"session_id":"abc123","title":"My session","created_at":"2026-06-15T18:24:05.412903+00:00","status":"idle","done_reason":null}
            ]}
        """.trimIndent()
        val response = json.decodeFromString(SessionsListResponseDto.serializer(), raw)

        assertEquals(1, response.sessions.size)
        val summary = response.sessions.single().toDomain()
        assertEquals("abc123", summary.sessionId)
        assertEquals("My session", summary.title)
        assertEquals("idle", summary.status)
        assertEquals("2026-06-15T18:24:05.412903+00:00", summary.createdAt)
        // updated_at was absent on the wire - falls back to created_at rather than going empty.
        assertEquals("2026-06-15T18:24:05.412903+00:00", summary.updatedAt)
        assertFalse(summary.recoverable)
        assertNull(summary.origin)
    }

    @Test
    fun `parses a session summary with nothing but the true identity present`() {
        val raw = """{"sessions":[{"session_id":"only-id"}]}"""
        val response = json.decodeFromString(SessionsListResponseDto.serializer(), raw)

        val summary = response.sessions.single().toDomain()
        assertEquals("only-id", summary.sessionId)
        assertEquals("", summary.status)
        assertEquals("", summary.createdAt)
        assertEquals("", summary.updatedAt)
        assertFalse(summary.recoverable)
    }

    /** Drawer running-dot regression (W2-B review): `running` is a separate boolean field on the
     * wire, not derivable from `status` - the live backend's `status` values never read "running". */
    @Test
    fun `maps the running boolean the drawer's running-dot gates on`() {
        val raw = """{"sessions":[{"session_id":"abc","status":"idle","running":true}]}"""
        val response = json.decodeFromString(SessionsListResponseDto.serializer(), raw)

        assertTrue(response.sessions.single().toDomain().running)
    }

    @Test
    fun `running defaults false when the field is absent from the wire`() {
        val raw = """{"sessions":[{"session_id":"abc"}]}"""
        val response = json.decodeFromString(SessionsListResponseDto.serializer(), raw)

        assertFalse(response.sessions.single().toDomain().running)
    }
}
