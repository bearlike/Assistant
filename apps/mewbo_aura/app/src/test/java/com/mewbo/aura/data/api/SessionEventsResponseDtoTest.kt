package com.mewbo.aura.data.api

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire round-trip for the `GET /api/sessions/{id}/events` status fields, specifically the
 * hard-termination signal (`terminated`/`terminated_at`) that
 * [com.mewbo.aura.ui.chat.ChatViewModel.bind] reads to open a permanently terminated session
 * directly into its terminal state (composer disabled, no Retry). The backend's `SessionEvents.get`
 * emits these from `summarize_session`.
 */
class SessionEventsResponseDtoTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `decodes the terminated status fields the events endpoint carries`() {
        val raw = """
            {"session_id":"dead","events":[],"running":false,"status":"terminated",
             "done_reason":null,"title":"Old chat","recoverable":false,
             "terminated":true,"terminated_at":"2026-07-13T20:00:00+00:00"}
        """.trimIndent()
        val response = json.decodeFromString(SessionEventsResponseDto.serializer(), raw)

        assertTrue(response.terminated)
        assertEquals("2026-07-13T20:00:00+00:00", response.terminatedAt)
        assertEquals("terminated", response.status)
        assertFalse(response.recoverable)
    }

    @Test
    fun `terminated defaults false and terminatedAt null for a normal live session`() {
        val raw = """{"session_id":"live","events":[],"running":true,"status":"idle"}"""
        val response = json.decodeFromString(SessionEventsResponseDto.serializer(), raw)

        assertFalse(response.terminated)
        assertNull(response.terminatedAt)
    }
}
