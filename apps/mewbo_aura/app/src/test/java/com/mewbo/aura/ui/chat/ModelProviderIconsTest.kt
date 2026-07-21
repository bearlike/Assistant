package com.mewbo.aura.ui.chat

import com.mewbo.aura.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [ModelProviderIcons.iconFor]/[ModelProviderIcons.providerNameFor] resolution - covers the
 * substring-match table mirrored from mewbo_console's `PROVIDER_ICONS`, the provider-prefix
 * insensitivity (a wire id's prefix does not reliably name the underlying provider - see
 * `data/CLAUDE.md`), and the `null` result an unrecognized id resolves to (the caller, not this
 * mapper, owns the generic `material-icons-extended` fallback glyph).
 */
class ModelProviderIconsTest {

    @Test
    fun `a claude family id resolves to the Anthropic glyph regardless of case`() {
        assertEquals(R.drawable.ic_provider_claude, ModelProviderIcons.iconFor("claude-sonnet-5"))
        assertEquals(R.drawable.ic_provider_claude, ModelProviderIcons.iconFor("Claude-Opus-4-8"))
        assertEquals("Anthropic", ModelProviderIcons.providerNameFor("claude-sonnet-5"))
    }

    @Test
    fun `a provider-prefixed id still matches on the family substring`() {
        // The API's own `default` field is prefixed ("openai/claude-sonnet-4-6") even for a
        // non-OpenAI-served model - normalizing away the prefix first is unnecessary because the
        // substring match sees straight through it.
        assertEquals(R.drawable.ic_provider_claude, ModelProviderIcons.iconFor("openai/claude-sonnet-4-6"))
    }

    @Test
    fun `gpt and openai both resolve to the OpenAI glyph, first match wins`() {
        assertEquals(R.drawable.ic_provider_openai, ModelProviderIcons.iconFor("gpt-oss-120b"))
        assertEquals(R.drawable.ic_provider_openai, ModelProviderIcons.iconFor("openai/text-embedding-3-small"))
        assertEquals("OpenAI", ModelProviderIcons.providerNameFor("gpt-oss-120b"))
    }

    @Test
    fun `zhipu, glm, z-dot-ai and zai every alias resolve to the same Zhipu AI glyph`() {
        for (id in listOf("zhipu-glm-4", "glm-4-plus", "z.ai-turbo", "zai-turbo")) {
            assertEquals(R.drawable.ic_provider_zhipu, ModelProviderIcons.iconFor(id))
            assertEquals("Zhipu AI", ModelProviderIcons.providerNameFor(id))
        }
    }

    @Test
    fun `an unrecognized id resolves to a null icon and the generic label, never a crash`() {
        assertNull(ModelProviderIcons.iconFor("some-self-hosted-model"))
        assertEquals("Model", ModelProviderIcons.providerNameFor("some-self-hosted-model"))
    }

    @Test
    fun `an empty id is safe and resolves to a null icon`() {
        assertNull(ModelProviderIcons.iconFor(""))
    }
}
