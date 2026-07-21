package com.mewbo.aura.data.api

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire round-trip for the rename/archive/recover/fork DTOs added for the drawer long-press sheet
 * (`PATCH .../title`, `POST .../archive`, `POST .../recover`, `POST .../fork`). Verified response
 * shapes: `{"session_id","title"}` / `{"session_id","archived"}` / `{"session_id","accepted",
 * "run_id"}` / `{"session_id","forked_from","forked_at"}`.
 */
class SessionMutationDtoTest {

    /** Mirrors `DataModule.provideJson()` EXACTLY (not just `ignoreUnknownKeys`) - the recover/fork
     * request tests below assert that a null field is DROPPED from the emitted body, which only
     * happens with `explicitNulls = false` (kotlinx.serialization's default is `true`, i.e. nulls
     * ARE encoded). Using a config that diverges from production here would validate a shape the
     * real Retrofit converter never actually emits. */
    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
    }

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

    @Test
    fun `RecoverSessionRequest serializes action and from_ts, dropping a null model`() {
        val body = json.encodeToString(
            RecoverSessionRequest.serializer(),
            RecoverSessionRequest(action = "retry", fromTs = "2026-07-01T00:00:00+00:00"),
        )
        assertEquals("""{"action":"retry","from_ts":"2026-07-01T00:00:00+00:00"}""", body)
    }

    @Test
    fun `RecoverSessionResponseDto parses the real 202 shape`() {
        val raw = """{"session_id":"s1","accepted":true,"run_id":"run-1"}"""
        val response = json.decodeFromString(RecoverSessionResponseDto.serializer(), raw)

        assertEquals("s1", response.sessionId)
        assertTrue(response.accepted)
        assertEquals("run-1", response.runId)
    }

    @Test
    fun `ForkSessionRequest omits both fields when null`() {
        val body = json.encodeToString(ForkSessionRequest.serializer(), ForkSessionRequest())
        assertEquals("{}", body)
    }

    @Test
    fun `ForkSessionResponseDto parses the real 201 shape`() {
        val raw = """{"session_id":"new-session","forked_from":"old-session","forked_at":"2026-07-01T00:00:00+00:00"}"""
        val response = json.decodeFromString(ForkSessionResponseDto.serializer(), raw)

        assertEquals("new-session", response.sessionId)
        assertEquals("old-session", response.forkedFrom)
        assertEquals("2026-07-01T00:00:00+00:00", response.forkedAt)
    }
}
