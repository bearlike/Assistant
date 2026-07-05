package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/** `GET api/models` capability entry for one model (`supports_vision` only, so far). */
@Immutable
data class ModelCapabilities(val supportsVision: Boolean)

/**
 * Popular/more partitioning + display-name derivation over the raw model list from `GET
 * api/models` (task brief W1-A). `@Immutable`, not just a plain data class, since [rawModels] and
 * [capabilities] are a `List`/`Map` - see [ChatItem]'s KDoc for why that annotation is load-bearing
 * for Compose stability once this sits inside `ChatUiState`.
 */
@Immutable
data class ModelCatalog(
    private val rawModels: List<String>,
    val default: String,
    private val capabilities: Map<String, ModelCapabilities>,
) {
    /** Ids containing "embedding" are never model-picker candidates (task brief). */
    private val candidates: List<String> = rawModels.filter { EMBEDDING !in it }

    /** One entry per matched [POPULAR_SUBSTRINGS] priority slot, newest-wins on ties (lexicographic
     * max - works for the current id scheme, e.g. "claude-sonnet-5" over "claude-sonnet-4-6").
     * Fewer than [POPULAR_SUBSTRINGS].size when a family isn't present in [rawModels]. */
    fun popular(): List<String> = POPULAR_SUBSTRINGS.mapNotNull { substring ->
        candidates.filter { substring in it }.maxOrNull()
    }

    /** Everything not pinned into [popular], sorted. */
    fun more(): List<String> {
        val pinned = popular().toSet()
        return candidates.filterNot { it in pinned }.sorted()
    }

    fun supportsVision(id: String): Boolean = capabilities[normalize(id)]?.supportsVision ?: false

    /** "claude-sonnet-5" -> "Claude Sonnet 5"; "claude-opus-4-8" -> "Claude Opus 4.8" (a purely
     * numeric run reads as a dotted version); "gpt-oss-120b" -> "GPT OSS 120B". */
    fun displayName(id: String): String = prettify(normalize(id).split("-"))

    /** Title-bar short form: drops a leading "claude" family word ("claude-sonnet-5" -> "Sonnet
     * 5"); other families ("gpt-oss-120b") keep every segment since there's no prefix to drop. */
    fun shortName(id: String): String {
        val words = normalize(id).split("-")
        val trimmed = if (words.size > 1 && words.first().equals("claude", ignoreCase = true)) {
            words.drop(1)
        } else {
            words
        }
        return prettify(trimmed)
    }

    /** [shortName] of [current], falling back to [default] when nothing has been explicitly
     * selected yet (`null` = "use the API default", [SettingsStore][com.mewbo.aura.data.settings.SettingsStore]'s convention). */
    fun effectiveShortName(current: String?): String = shortName(current ?: default)

    /** True when raw catalog entry [id] is the effective selection - [current] is the user's
     * persisted choice, or `null` to mean [default]. Ids are compared normalized so a prefixed
     * default ("openai/claude-sonnet-4-6") matches its bare catalog entry. */
    fun isSelected(id: String, current: String?): Boolean = normalize(id) == normalize(current ?: default)

    companion object {
        private const val EMBEDDING = "embedding"

        /** Priority order: first match per substring wins a pinned slot. */
        private val POPULAR_SUBSTRINGS = listOf("claude-sonnet", "claude-haiku", "claude-opus", "gpt-oss-120b")

        /** Strips a provider prefix ("openai/claude-sonnet-4-6" -> "claude-sonnet-4-6") - the API's
         * `default` field is prefixed, `models` list entries are bare. */
        fun normalize(id: String): String = id.substringAfterLast('/')

        /** Known all-caps families; every other alphabetic segment title-cases. */
        private val ACRONYMS = setOf("gpt", "oss", "glm")

        /** Matches a parameter/size tail like "120b"/"32b" (uppercased for display). */
        private val SIZE_TAIL = Regex("""\d+[a-z]""")

        /** Title-cases alphabetic segments (acronyms uppercase); adjacent purely-numeric segments
         * (a version suffix like "4"/"8") join with "." to read as a version; a size tail like
         * "120b" uppercases. Deterministic, no per-family special-casing. */
        private fun prettify(segments: List<String>): String {
            val words = mutableListOf<String>()
            val versionRun = mutableListOf<String>()
            fun flushVersionRun() {
                if (versionRun.isNotEmpty()) {
                    val joiner = if (versionRun.all { run -> run.all(Char::isDigit) }) "." else "-"
                    words += versionRun.joinToString(joiner) {
                        if (SIZE_TAIL.matches(it)) it.uppercase() else it
                    }
                    versionRun.clear()
                }
            }
            for (segment in segments) {
                if (segment.firstOrNull()?.isDigit() == true) {
                    versionRun += segment
                } else {
                    flushVersionRun()
                    words += if (segment.lowercase() in ACRONYMS) {
                        segment.uppercase()
                    } else {
                        segment.replaceFirstChar(Char::uppercase)
                    }
                }
            }
            flushVersionRun()
            return words.joinToString(" ")
        }
    }
}
