package com.mewbo.aura.ui.chat

import androidx.annotation.DrawableRes
import com.mewbo.aura.R

/**
 * Model-id -> provider brand-icon mapping for the [ModelPickerSheet] rows. Mirrors
 * mewbo_console's `PROVIDER_ICONS` match table (`src/utils/modelIcon.ts`) so both clients agree
 * on which model ids get a bespoke glyph: first match wins, matched against the lowercased,
 * un-normalized model id (a provider prefix like "openai/claude-…" still contains "claude" as a
 * substring, so normalizing first is unnecessary - see `data/CLAUDE.md`'s note that the wire's
 * own prefix does not reliably name the underlying provider).
 *
 * Icons are hand-converted from `@lobehub/icons`' MIT-licensed monochrome SVGs (single
 * `currentColor` path, 24x24 viewBox - `res/drawable/ic_provider_*.xml`), never the colored/
 * gradient variants: DESIGN.md's theme discipline wants a themed tint, not a baked brand hex.
 * There is deliberately no bespoke "generic" drawable in this set - an unrecognized model id
 * returns `null` from [iconFor] and the caller falls back to a `material-icons-extended` glyph
 * (orchestrator directive 2026-07-14: no hand-rolled icon when an off-the-shelf one covers the
 * case - a brand LOGO still has to be hand-converted since Material has no such thing, but a
 * plain "this is some model" glyph does not).
 */
object ModelProviderIcons {
    private class ProviderIcon(val match: String, val providerName: String, @DrawableRes val iconRes: Int)

    private val PROVIDER_ICONS = listOf(
        ProviderIcon("claude", "Anthropic", R.drawable.ic_provider_claude),
        ProviderIcon("gemini", "Gemini", R.drawable.ic_provider_gemini),
        ProviderIcon("deepseek", "DeepSeek", R.drawable.ic_provider_deepseek),
        ProviderIcon("llama", "Meta", R.drawable.ic_provider_meta),
        ProviderIcon("minimax", "MiniMax", R.drawable.ic_provider_minimax),
        ProviderIcon("qwen", "Qwen", R.drawable.ic_provider_qwen),
        ProviderIcon("zhipu", "Zhipu AI", R.drawable.ic_provider_zhipu),
        ProviderIcon("glm", "Zhipu AI", R.drawable.ic_provider_zhipu),
        ProviderIcon("z.ai", "Zhipu AI", R.drawable.ic_provider_zhipu),
        ProviderIcon("zai", "Zhipu AI", R.drawable.ic_provider_zhipu),
        ProviderIcon("gpt", "OpenAI", R.drawable.ic_provider_openai),
        ProviderIcon("openai", "OpenAI", R.drawable.ic_provider_openai),
    )

    private fun matchFor(modelId: String): ProviderIcon? {
        val lower = modelId.lowercase()
        return PROVIDER_ICONS.firstOrNull { it.match in lower }
    }

    /** First [PROVIDER_ICONS] entry whose [ProviderIcon.match] is a substring of [modelId]
     * (case-insensitive), or `null` when nothing matches - the caller renders a generic
     * `material-icons-extended` glyph in that case, never a blank leading slot. */
    @DrawableRes
    fun iconFor(modelId: String): Int? = matchFor(modelId)?.iconRes

    /** Accessible label for the glyph [iconFor] resolves - the matched provider's display name,
     * or "Model" when [iconFor] returns `null`. */
    fun providerNameFor(modelId: String): String = matchFor(modelId)?.providerName ?: "Model"
}
