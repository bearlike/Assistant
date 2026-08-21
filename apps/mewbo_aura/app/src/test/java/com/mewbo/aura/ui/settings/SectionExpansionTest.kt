package com.mewbo.aura.ui.settings

import androidx.compose.runtime.saveable.SaverScope
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The screen opens quiet, and a rotation does not slam it shut under the user. */
class SectionExpansionTest {

    @Test
    fun `every section starts collapsed`() {
        val expansion = SectionExpansion()
        listOf("connection", "defaults", "permissions", "tools").forEach {
            assertFalse("$it must start collapsed", expansion.isOpen(it))
        }
    }

    @Test
    fun `sections open independently, so opening one never closes another`() {
        val expansion = SectionExpansion()
        expansion.toggle("permissions")
        expansion.toggle("tools")
        assertTrue(expansion.isOpen("permissions"))
        assertTrue(expansion.isOpen("tools"))
        expansion.toggle("permissions")
        assertFalse(expansion.isOpen("permissions"))
        assertTrue(expansion.isOpen("tools"))
    }

    @Test
    fun `what was open survives a save and restore`() {
        val expansion = SectionExpansion()
        expansion.toggle("permissions")

        val saver = SectionExpansion.Saver
        val saved = with(saver) { SaverScope { true }.save(expansion) }
        val restored = saver.restore(requireNotNull(saved))

        assertEquals(true, restored?.isOpen("permissions"))
        assertEquals(false, restored?.isOpen("tools"))
    }
}
