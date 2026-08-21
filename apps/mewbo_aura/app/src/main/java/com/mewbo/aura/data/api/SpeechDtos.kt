package com.mewbo.aura.data.api

import com.mewbo.aura.data.model.SpeechCatalog
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.data.model.SpeechEngineOption
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * The `/api/speech` wire contract, VERIFIED field-for-field against the server's own route module
 * (`apps/mewbo_api/src/mewbo_api/speech/routes.py`) rather than inferred.
 *
 * This file and [com.mewbo.aura.data.repo.SpeechRepository] are the only two places that know any
 * of it: `voice/` talks to [com.mewbo.aura.voice.SpeechGateway] and the settings screen reads a
 * [SpeechCatalog], both of which are ours.
 *
 * **The synthesize body is `extra="forbid"` server-side, so a stray field is a 400, not a
 * no-op.** That cuts both ways and is why the names here are checked rather than guessed: an
 * earlier draft of this client sent `audio_format`, which is the speech package's INTERNAL field
 * name — the route calls it `response_format` and would have rejected every request naming the
 * other one. Add a field here only after reading `SynthesizeBody`.
 *
 * Decode tolerantly all the same ([`data/api/CLAUDE.md`](CLAUDE.md)): every field defaults, so a
 * response gaining a key we do not read still decodes.
 */
@Serializable
data class SpeechCapabilitiesResponseDto(
    val synthesis: SpeechDirectionDto = SpeechDirectionDto(),
    val transcription: SpeechDirectionDto = SpeechDirectionDto(),
) {
    /**
     * Both directions folded into one catalog.
     *
     * **A direction reporting `available = false` contributes NO models**, even if it listed
     * some. The server sets that flag when the gateway is unreachable or no model is configured,
     * and it keeps answering the rest of the payload so a client can still render a picker — so
     * the list alone is not the availability signal. Offering an engine the server has just said
     * it cannot serve would produce a selection that fails on every use with no way to tell why.
     */
    fun toCatalog(): SpeechCatalog = SpeechCatalog(
        synthesis.options(SpeechDirection.TextToSpeech) +
            transcription.options(SpeechDirection.SpeechToText),
    )
}

@Serializable
data class SpeechDirectionDto(
    val available: Boolean = false,
    val models: List<SpeechModelDto> = emptyList(),
) {
    fun options(direction: SpeechDirection): List<SpeechEngineOption> =
        if (!available) emptyList() else models.mapNotNull { it.toDomain(direction) }
}

/**
 * One advertised engine. `mode` rides each entry too, but the grouping under
 * `synthesis`/`transcription` is what this client reads — it is the server's own partitioning, so
 * consulting it cannot disagree with itself the way a second mode-string mapping could.
 */
@Serializable
data class SpeechModelDto(
    val id: String = "",
    @SerialName("display_name") val displayName: String = "",
) {
    /** `null` for a blank id — an unnameable engine is not offerable. */
    fun toDomain(direction: SpeechDirection): SpeechEngineOption? {
        if (id.isBlank()) return null
        return SpeechEngineOption(id = id, label = displayName.ifBlank { id }, direction = direction)
    }
}

/**
 * `POST /api/speech/synthesize`. The response is RAW AUDIO BYTES — see
 * [AuraApi.synthesizeSpeech].
 *
 * `voice` and `responseFormat` are deliberately left null: the server applies the operator's
 * configured defaults when a field is absent, which keeps config the single source of truth for
 * what "unspecified" means. `explicitNulls = false` on this app's `Json`
 * ([com.mewbo.aura.di.DataModule]) is what makes absent mean absent — with it on, a `null` would
 * be SERIALIZED, and `extra="forbid"` accepts the key but the value would override nothing
 * usefully. Do not "fix" that Json setting without re-reading this.
 */
@Serializable
data class SpeechSynthesizeRequest(
    val text: String,
    val model: String,
    val voice: String? = null,
    @SerialName("response_format") val responseFormat: String? = null,
)

/** `POST /api/speech/transcribe`. `model` echoes which engine served it; only `text` is read. */
@Serializable
data class SpeechTranscribeResponseDto(
    val text: String = "",
    val model: String = "",
)
