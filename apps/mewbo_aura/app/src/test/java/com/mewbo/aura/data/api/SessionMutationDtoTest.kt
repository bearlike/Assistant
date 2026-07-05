package com.mewbo.aura.data.api

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire round-trip for the rename/archive DTOs added for the drawer long-press sheet (`PATCH
 * .../title`, `POST .../archive`). Verified response shape: `{"session_id","title"}` /
 * `{"session_id","archived"}`.
 */
class SessionMutationDtoTest {

    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `RenameSessionRequest serializes the title field verbatim`() {
        val body = json.encodeToString(RenameSessionRequest.serializer(), RenameSessionRequest("New title"))
        assertEquals("""{"title":"New title"}""", body)
    }

    @Test
    fun `RenameSessionResponseDto parses the real success shape`() {
        val raw = """{"session_id":"abc123","title":"Renamed"}"""
        val response = json.decodeFromString(RenameSessionResponseDto.serializer(), raw)

        assertEquals("abc123", response.sessionId)
        assertEquals("Renamed", response.title)
    }

    @Test
    fun `RenameSessionResponseDto tolerates a missing title`() {
        val raw = """{"session_id":"abc123"}"""
        val response = json.decodeFromString(RenameSessionResponseDto.serializer(), raw)

        assertEquals("abc123", response.sessionId)
        assertNull(response.title)
    }

    @Test
    fun `ArchiveSessionResponseDto parses the real success shape`() {
        val raw = """{"session_id":"abc123","archived":true}"""
        val response = json.decodeFromString(ArchiveSessionResponseDto.serializer(), raw)

        assertEquals("abc123", response.sessionId)
        assertTrue(response.archived)
    }

    @Test
    fun `ArchiveSessionResponseDto defaults archived false when absent`() {
        val raw = """{"session_id":"abc123"}"""
        val response = json.decodeFromString(ArchiveSessionResponseDto.serializer(), raw)

        assertFalse(response.archived)
    }
}
