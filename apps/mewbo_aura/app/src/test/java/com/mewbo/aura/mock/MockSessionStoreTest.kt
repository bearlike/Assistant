package com.mewbo.aura.mock

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [MockSessionStore.rename]/[MockSessionStore.archive] back [MockBackendInterceptor]'s `PATCH
 * .../title` and `POST .../archive` handlers - covers the hide-not-delete semantics [MockSessionStore.list]
 * must honor (mirrors the real backend: `GET /api/sessions` hides archived rows unless
 * `include_archived=true`, per `data/CLAUDE.md`).
 */
class MockSessionStoreTest {

    @Test
    fun `rename mutates the stored session's title`() {
        val store = MockSessionStore()
        val record = store.create()

        store.rename(record.sessionId, "Renamed")

        assertEquals("Renamed", store.get(record.sessionId)?.title)
    }

    @Test
    fun `rename on an unknown session id is a harmless no-op`() {
        val store = MockSessionStore()

        store.rename("does-not-exist", "Renamed")

        assertEquals(null, store.get("does-not-exist"))
    }

    @Test
    fun `archive hides the session from the default list but not from get`() {
        val store = MockSessionStore()
        val record = store.create()

        store.archive(record.sessionId)

        assertTrue(store.list().none { it.sessionId == record.sessionId })
        assertTrue(store.get(record.sessionId)?.archived == true)
    }

    @Test
    fun `list with includeArchived surfaces archived sessions too`() {
        val store = MockSessionStore()
        val record = store.create()
        store.archive(record.sessionId)

        val ids = store.list(includeArchived = true).map { it.sessionId }

        assertTrue(ids.contains(record.sessionId))
    }

    @Test
    fun `an unarchived session is present in both list forms`() {
        val store = MockSessionStore()
        val record = store.create()

        assertTrue(store.list().any { it.sessionId == record.sessionId })
        assertTrue(store.list(includeArchived = true).any { it.sessionId == record.sessionId })
        assertFalse(record.archived)
    }
}
