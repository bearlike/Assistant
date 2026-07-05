package com.mewbo.aura.data.repo

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [ConnectionProbe.mapResponse] is the wire-mapping half of [ConnectionProbe.validate] split out
 * specifically so this doesn't need a real socket (no MockWebServer dependency in this module) -
 * stubs the transport boundary by calling it directly with a status code + body, same shape
 * `validate()` derives from a real [okhttp3.Response].
 */
class ConnectionProbeTest {

    private val probe = ConnectionProbe(Json { ignoreUnknownKeys = true })

    @Test
    fun `200 with a models list maps to Ok with the model count`() {
        val body = """{"models":["gpt-5.4","opus-4.8","haiku-4.5"],"default":"gpt-5.4","capabilities":{}}"""

        val result = probe.mapResponse(200, body)

        assertEquals(ConnectionProbe.Result.Ok(3), result)
    }

    @Test
    fun `200 with an empty models list maps to Ok with a zero count`() {
        val result = probe.mapResponse(200, """{"models":[]}""")

        assertEquals(ConnectionProbe.Result.Ok(0), result)
    }

    @Test
    fun `401 maps to Http regardless of body`() {
        val result = probe.mapResponse(401, "")

        assertEquals(ConnectionProbe.Result.Http(401), result)
    }

    @Test
    fun `non-200 status is never parsed as a body, even if it happens to be valid JSON`() {
        val result = probe.mapResponse(500, """{"models":["gpt-5.4"]}""")

        assertEquals(ConnectionProbe.Result.Http(500), result)
    }

    @Test
    fun `200 with a malformed body maps to Unreachable, not a crash`() {
        val result = probe.mapResponse(200, "not json")

        assertTrue(result is ConnectionProbe.Result.Unreachable)
    }
}
