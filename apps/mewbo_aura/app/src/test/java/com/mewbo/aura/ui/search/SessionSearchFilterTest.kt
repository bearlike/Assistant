package com.mewbo.aura.ui.search

import com.mewbo.aura.data.model.SessionSummary
import org.junit.Assert.assertEquals
import org.junit.Test

class SessionSearchFilterTest {

    private fun session(id: String, title: String?) = SessionSummary(
        sessionId = id,
        title = title,
        status = "idle",
        running = false,
        doneReason = null,
        origin = null,
        recoverable = true,
        createdAt = "2026-07-01T00:00:00Z",
        updatedAt = "2026-07-01T00:00:00Z",
    )

    @Test
    fun `empty query returns every session unfiltered`() {
        val sessions = listOf(session("a", "Alarm"), session("b", "Weather"))

        assertEquals(sessions, SessionSearchFilter.filter(sessions, ""))
    }

    @Test
    fun `blank query (whitespace only) returns every session`() {
        val sessions = listOf(session("a", "Alarm"))

        assertEquals(sessions, SessionSearchFilter.filter(sessions, "   "))
    }

    @Test
    fun `query matches case-insensitively against the title`() {
        val sessions = listOf(session("a", "Setting an Alarm"), session("b", "Weather forecast"))

        val result = SessionSearchFilter.filter(sessions, "alarm")

        assertEquals(listOf(sessions[0]), result)
    }

    @Test
    fun `query with no matches returns an empty list`() {
        val sessions = listOf(session("a", "Setting an Alarm"))

        assertEquals(emptyList<SessionSummary>(), SessionSearchFilter.filter(sessions, "xyz"))
    }

    @Test
    fun `sessions with a null title never match a non-empty query`() {
        val sessions = listOf(session("a", null))

        assertEquals(emptyList<SessionSummary>(), SessionSearchFilter.filter(sessions, "untitled"))
    }
}
