package com.mewbo.aura.data.model

import com.mewbo.aura.data.api.SpeechCapabilitiesResponseDto
import com.mewbo.aura.data.api.SpeechDirectionDto
import com.mewbo.aura.data.api.SpeechModelDto
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What the two speech rows and their pickers actually read.
 *
 * The display naming is where the privacy claim lives: a row saying "On device" over a server
 * engine is the wrong-green this settings screen exists to prevent, and unlike a permission badge
 * nothing on the device would ever contradict it.
 */
class SpeechCatalogTest {

    private val catalog = SpeechCatalog(
        listOf(
            SpeechEngineOption("supertonic-3", "Supertonic 3", SpeechDirection.TextToSpeech),
            SpeechEngineOption("nova-3", "Nova 3", SpeechDirection.SpeechToText),
            SpeechEngineOption("aria-1", "Aria 1", SpeechDirection.TextToSpeech),
        ),
    )

    @Test
    fun `an unset selection reads as on device in both directions`() {
        assertEquals(
            SpeechCatalog.ON_DEVICE_LABEL,
            catalog.displayName(SpeechCatalog.ON_DEVICE, SpeechDirection.SpeechToText),
        )
        assertEquals(
            SpeechCatalog.ON_DEVICE_LABEL,
            catalog.displayName(SpeechCatalog.ON_DEVICE, SpeechDirection.TextToSpeech),
        )
    }

    @Test
    fun `a server engine is always marked, so the row states that audio leaves the device`() {
        val label = catalog.displayName("nova-3", SpeechDirection.SpeechToText)

        assertTrue("expected the cloud mark in <$label>", label.startsWith(SpeechCatalog.CLOUD_MARK))
        assertTrue(label.contains("Nova 3"))
    }

    @Test
    fun `an unknown id degrades to the marked raw id, never to On device`() {
        // Happens with a stale selection, or before the catalog has loaded. Falling back to
        // "On device" here would claim the opposite of the truth about where audio goes.
        val label = SpeechCatalog(emptyList()).displayName("whisper-9", SpeechDirection.SpeechToText)

        assertEquals("${SpeechCatalog.CLOUD_MARK} whisper-9", label)
    }

    @Test
    fun `a direction only ever offers its own engines`() {
        val toText = catalog.serverOptions(SpeechDirection.SpeechToText).map { it.id }
        val toSpeech = catalog.serverOptions(SpeechDirection.TextToSpeech).map { it.id }

        assertEquals(listOf("nova-3"), toText)
        assertEquals("sorted by label", listOf("aria-1", "supertonic-3"), toSpeech)
    }

    @Test
    fun `an id matching the other direction is not resolved`() {
        // `supertonic-3` is a synthesis model; asked for as a recognizer it must not borrow that
        // label, or the STT row would name an engine it can never use.
        val label = catalog.displayName("supertonic-3", SpeechDirection.SpeechToText)

        assertEquals("${SpeechCatalog.CLOUD_MARK} supertonic-3", label)
    }

    // ---- the wire mapping, verified against `mewbo_api/speech/routes.py` ----

    @Test
    fun `the server's own grouping decides the direction`() {
        val response = SpeechCapabilitiesResponseDto(
            synthesis = SpeechDirectionDto(true, listOf(SpeechModelDto("supertonic-3", "Supertonic 3"))),
            transcription = SpeechDirectionDto(true, listOf(SpeechModelDto("nova-3", "Nova 3"))),
        )

        val built = response.toCatalog()

        assertEquals(listOf("supertonic-3"), built.serverOptions(SpeechDirection.TextToSpeech).map { it.id })
        assertEquals(listOf("nova-3"), built.serverOptions(SpeechDirection.SpeechToText).map { it.id })
    }

    @Test
    fun `an unavailable direction contributes nothing even when it lists models`() {
        // The route keeps answering defaults and models with `available: false` when the gateway
        // is unreachable, so the LIST is not the availability signal. Offering one of these would
        // give the user a selection that fails on every use with nothing to explain it.
        val response = SpeechCapabilitiesResponseDto(
            synthesis = SpeechDirectionDto(
                available = false,
                models = listOf(SpeechModelDto("supertonic-3", "Supertonic 3")),
            ),
            transcription = SpeechDirectionDto(true, listOf(SpeechModelDto("nova-3", "Nova 3"))),
        )

        val built = response.toCatalog()

        assertTrue(built.serverOptions(SpeechDirection.TextToSpeech).isEmpty())
        assertEquals(listOf("nova-3"), built.serverOptions(SpeechDirection.SpeechToText).map { it.id })
    }

    @Test
    fun `a deployment without the speech package yields an empty catalog, not a crash`() {
        // The whole namespace is optional server-side; every field defaults, so an absent or
        // unrecognised payload decodes to both directions unavailable.
        val built = SpeechCapabilitiesResponseDto().toCatalog()

        assertTrue(built.serverOptions(SpeechDirection.TextToSpeech).isEmpty())
        assertTrue(built.serverOptions(SpeechDirection.SpeechToText).isEmpty())
    }

    @Test
    fun `a blank id is dropped, and a blank display name falls back to the id`() {
        assertNull(SpeechModelDto("", "Nameless").toDomain(SpeechDirection.TextToSpeech))
        assertEquals("nova-3", SpeechModelDto("nova-3").toDomain(SpeechDirection.SpeechToText)?.label)
    }
}
