package com.mewbo.aura.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Coverage for the model-picker partitioning/prettify rules (task brief W1-A). */
class ModelCatalogTest {

    private val raw = listOf(
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-opus-4-8",
        "gpt-oss-120b",
        "gpt-4o-mini",
        "text-embedding-3-large",
    )
    private val catalog = ModelCatalog(
        rawModels = raw,
        default = "openai/claude-sonnet-4-6",
        capabilities = mapOf(
            "claude-sonnet-5" to ModelCapabilities(supportsVision = true),
            "gpt-4o-mini" to ModelCapabilities(supportsVision = false),
        ),
    )

    // ---- normalize ----

    @Test
    fun `normalize strips a provider prefix`() {
        assertEquals("claude-sonnet-4-6", ModelCatalog.normalize("openai/claude-sonnet-4-6"))
    }

    @Test
    fun `normalize is a no-op on a bare id`() {
        assertEquals("claude-sonnet-5", ModelCatalog.normalize("claude-sonnet-5"))
    }

    // ---- popular - partitioning + newest-wins ----

    @Test
    fun `popular pins one entry per family, newest (lexicographic max) wins the sonnet tie`() {
        assertEquals(
            listOf("claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8", "gpt-oss-120b"),
            catalog.popular(),
        )
    }

    @Test
    fun `popular skips a family that has no matching id`() {
        val noOpus = ModelCatalog(
            rawModels = listOf("claude-sonnet-5", "claude-haiku-4-5"),
            default = "claude-sonnet-5",
            capabilities = emptyMap(),
        )
        assertEquals(listOf("claude-sonnet-5", "claude-haiku-4-5"), noOpus.popular())
    }

    // ---- embedding filter ----

    @Test
    fun `embedding ids never appear in popular or more`() {
        assertFalse(catalog.popular().any { "embedding" in it })
        assertFalse(catalog.more().any { "embedding" in it })
    }

    // ---- more ----

    @Test
    fun `more contains the losing tie entry and unrelated ids, sorted, excluding pinned`() {
        assertEquals(listOf("claude-sonnet-4-6", "gpt-4o-mini"), catalog.more())
    }

    // ---- displayName / shortName ----

    @Test
    fun `displayName title-cases words, dots version runs, uppercases acronyms`() {
        assertEquals("Claude Sonnet 5", catalog.displayName("claude-sonnet-5"))
        assertEquals("Claude Opus 4.8", catalog.displayName("claude-opus-4-8"))
        assertEquals("GPT OSS 120B", catalog.displayName("gpt-oss-120b"))
    }

    @Test
    fun `shortName drops a leading claude family word`() {
        assertEquals("Sonnet 5", catalog.shortName("claude-sonnet-5"))
        assertEquals("Opus 4.8", catalog.shortName("claude-opus-4-8"))
    }

    @Test
    fun `shortName keeps every segment when there is no claude prefix to drop`() {
        assertEquals("GPT OSS 120B", catalog.shortName("gpt-oss-120b"))
    }

    @Test
    fun `effectiveShortName falls back to the (prefixed) default when nothing is selected`() {
        assertEquals("Sonnet 4.6", catalog.effectiveShortName(null))
    }

    @Test
    fun `effectiveShortName reflects an explicit selection over the default`() {
        assertEquals("Opus 4.8", catalog.effectiveShortName("claude-opus-4-8"))
    }

    // ---- isSelected ----

    @Test
    fun `isSelected matches a bare id against the prefixed default when nothing is chosen`() {
        assertTrue(catalog.isSelected("claude-sonnet-4-6", current = null))
        assertFalse(catalog.isSelected("claude-sonnet-5", current = null))
    }

    @Test
    fun `isSelected matches an explicit selection`() {
        assertTrue(catalog.isSelected("claude-sonnet-5", current = "claude-sonnet-5"))
        assertFalse(catalog.isSelected("claude-sonnet-4-6", current = "claude-sonnet-5"))
    }

    // ---- supportsVision ----

    @Test
    fun `supportsVision reads the capability map by normalized id`() {
        assertTrue(catalog.supportsVision("claude-sonnet-5"))
        assertFalse(catalog.supportsVision("gpt-4o-mini"))
        assertFalse(catalog.supportsVision("claude-opus-4-8"))
    }
}
