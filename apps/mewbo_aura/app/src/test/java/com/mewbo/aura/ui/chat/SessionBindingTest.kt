package com.mewbo.aura.ui.chat

import com.mewbo.aura.data.model.ComposerScope
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Regression coverage for task integ bug A: a shared [ChatViewModel] instance persists across
 * "New chat" / drawer session-switch navigations (`ui/navigation/AuraNavHost.kt`'s single-
 * destination design), so [SessionBinding.rebindTo] - not a one-shot "bind once ever" guard - is
 * what has to correctly distinguish a genuine session switch from a redundant re-invocation. The
 * bug: with the original one-shot shape, a send from a "fresh" greeting after any prior session
 * had bound would steer into that stale session instead of creating a new one.
 */
class SessionBindingTest {

    @Test
    fun `first ever bind with a null id (cold-start greeting) proceeds and records null`() {
        val binding = SessionBinding()
        assertTrue(binding.rebindTo(null))
        assertEquals(null, binding.currentId)
    }

    @Test
    fun `first ever bind with a real id (opening a session from the drawer) proceeds and records it`() {
        val binding = SessionBinding()
        assertTrue(binding.rebindTo("session-abc"))
        assertEquals("session-abc", binding.currentId)
    }

    @Test
    fun `rebinding to a different id after already being bound proceeds - the bug A regression`() {
        val binding = SessionBinding()
        binding.rebindTo("session-abc")
        assertTrue(binding.rebindTo("session-xyz"))
        assertEquals("session-xyz", binding.currentId)
    }

    @Test
    fun `New chat after an existing session - switching TO null - proceeds and clears currentId`() {
        val binding = SessionBinding()
        binding.rebindTo("session-abc")
        assertTrue(binding.rebindTo(null))
        assertEquals(null, binding.currentId)
    }

    @Test
    fun `opening a real session from a fresh greeting - switching FROM null - proceeds`() {
        val binding = SessionBinding()
        binding.rebindTo(null)
        assertTrue(binding.rebindTo("session-xyz"))
        assertEquals("session-xyz", binding.currentId)
    }

    @Test
    fun `a redundant rebind to the SAME already-bound id is a no-op`() {
        val binding = SessionBinding()
        binding.rebindTo("session-abc")
        assertFalse(binding.rebindTo("session-abc"))
        assertEquals("session-abc", binding.currentId)
    }

    @Test
    fun `a redundant rebind to null while already on a fresh greeting is a no-op`() {
        val binding = SessionBinding()
        binding.rebindTo(null)
        assertFalse(binding.rebindTo(null))
        assertEquals(null, binding.currentId)
    }

    @Test
    fun `currentId set directly (ChatViewModel send creating a session from a fresh chat) is NOT reverted by a later same-id rebind`() {
        // Mirrors ChatViewModel.send(): `binding.currentId = binding.currentId ?: sessionRepository.createSession()...`
        // sets currentId WITHOUT going through rebindTo - a later bind() call for that same id (e.g. a
        // recomposition) must still see it as "already bound", not force a spurious reset.
        val binding = SessionBinding()
        binding.rebindTo(null)
        binding.currentId = "session-created-by-send"
        assertFalse(binding.rebindTo("session-created-by-send"))
    }
}

/**
 * Coverage for [reseedProjectForBind] (Gitea #178 W1-A: default-project seeding) - a plain pure
 * function, so unlike [SessionBinding] itself it needs no fixture at all, just direct calls.
 */
class ReseedProjectForBindTest {

    @Test
    fun `a fresh chat (null id) reseeds selectedProjectKey from the default`() {
        val current = ComposerScope(selectedProjectKey = "some-stale-value")
        val reseeded = reseedProjectForBind(id = null, current = current, defaultProjectKey = "managed:abc123")
        assertEquals("managed:abc123", reseeded.selectedProjectKey)
    }

    @Test
    fun `a fresh chat reseeds to null (Temporary) when there is no default project set`() {
        val current = ComposerScope(selectedProjectKey = "some-stale-value")
        val reseeded = reseedProjectForBind(id = null, current = current, defaultProjectKey = null)
        assertNull(reseeded.selectedProjectKey)
    }

    @Test
    fun `binding an existing session leaves the current scope untouched - a per-chat override still wins`() {
        val current = ComposerScope(selectedProjectKey = "picked-in-this-chat")
        val reseeded = reseedProjectForBind(id = "session-abc", current = current, defaultProjectKey = "some-other-default")
        assertEquals("picked-in-this-chat", reseeded.selectedProjectKey)
    }
}
