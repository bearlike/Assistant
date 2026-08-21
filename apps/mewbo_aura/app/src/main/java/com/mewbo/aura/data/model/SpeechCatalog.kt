package com.mewbo.aura.data.model

import androidx.compose.runtime.Immutable

/**
 * Which direction a speech engine runs in. The two are chosen independently — a user can dictate
 * on-device and hear a server voice, or the reverse — so nothing anywhere pairs them.
 */
enum class SpeechDirection {
    /** Microphone in, text out. */
    SpeechToText,

    /** Text in, audio out. */
    TextToSpeech,
}

/**
 * One server-provided speech engine, as offered in a picker.
 *
 * There is no on-device member: the on-device engine is the ABSENCE of a stored id
 * ([SpeechCatalog.ON_DEVICE]), the same empty-means-default convention
 * [com.mewbo.aura.data.settings.SettingsStore.selectedModel] already uses. Modelling it as an
 * option too would give "on device" two spellings — an empty id and a sentinel id — and a stored
 * sentinel would then need migrating the day the sentinel changes.
 */
@Immutable
data class SpeechEngineOption(
    val id: String,
    val label: String,
    val direction: SpeechDirection,
)

/**
 * The server's speech engines, partitioned by direction, plus the display naming both settings
 * rows and both picker sheets read.
 *
 * Mirrors [ModelCatalog]'s shape deliberately: a catalog fetched fresh (never persisted — only the
 * SELECTION persists), `null` at the call site until a picker first opens, and a display-name
 * resolver that degrades to the raw id rather than failing when the catalog has not loaded.
 */
@Immutable
data class SpeechCatalog(private val options: List<SpeechEngineOption>) {

    /** **Cost: `O(collection)` over an already-fetched list** — bounded by the gateway's own model
     * count (tens), and this is a filter over memory, never a fetch. */
    fun serverOptions(direction: SpeechDirection): List<SpeechEngineOption> =
        options.filter { it.direction == direction }.sortedBy { it.label }

    /**
     * How [storedId] reads in a settings row and in a picker.
     *
     * A blank id is the on-device engine and reads as such. A known server id gets its label; an
     * id the catalog does not carry (stale selection, or the catalog has not loaded) degrades to
     * the raw id — the same posture [resolveModelDisplayName][com.mewbo.aura.ui.settings] takes,
     * so a row stays informative offline instead of going blank or claiming "On device" for a
     * server engine the user actually picked.
     */
    fun displayName(storedId: String, direction: SpeechDirection): String =
        if (isOnDevice(storedId)) {
            ON_DEVICE_LABEL
        } else {
            cloudLabel(options.firstOrNull { it.id == storedId && it.direction == direction }?.label ?: storedId)
        }

    companion object {
        /** A blank stored id means the on-device engine, which is also the default for both
         * directions — so a user who never opens the setting keeps the platform behaviour. */
        const val ON_DEVICE = ""

        const val ON_DEVICE_LABEL = "On device"

        /**
         * Marks every server engine wherever one is named.
         *
         * The point is that the choice is a PRIVACY fact before it is a quality one: picking a
         * server engine sends microphone audio, or the text of a reply, off the device. The mark
         * rides the label itself rather than a separate tint or badge so it survives into every
         * surface that renders the name — a picker row, a collapsed settings row, a TalkBack
         * announcement — instead of existing only where someone remembered to add a second signal.
         */
        const val CLOUD_MARK = "☁️"

        fun isOnDevice(storedId: String): Boolean = storedId.isBlank()

        /** The one place the mark is attached, so a picker row and a settings row can never
         * disagree about whether an engine is a server one. */
        fun cloudLabel(label: String): String = "$CLOUD_MARK $label"
    }
}
