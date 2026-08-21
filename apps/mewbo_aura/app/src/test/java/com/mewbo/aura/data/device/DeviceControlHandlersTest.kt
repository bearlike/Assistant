package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlBinder
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.device.shizuku.DeviceControlStatusSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What the model actually reads back. The union is only useful if its
 * discriminator survives the trip through JSON — a refusal that arrives as
 * prose is a refusal the model has to guess at.
 */
class DeviceControlHandlersTest {

    private fun session(status: DeviceControlStatus = DeviceControlStatus.Ready) =
        DeviceControlSession(
            DeviceControlStatusSource { MutableStateFlow(status) },
            DeviceControlBinder { true },
            CoroutineScope(Dispatchers.Unconfined),
        )

    private val noArgs = buildJsonObject {}

    @Test
    fun `start reports the discriminator, the state and a message`() = runTest {
        val result = DeviceControlStartHandler(session()).execute(noArgs)

        assertEquals("granted", result["outcome"]?.jsonPrimitive?.content)
        assertTrue(result["active"]!!.jsonPrimitive.boolean)
        assertTrue(result["message"]!!.jsonPrimitive.content.isNotBlank())
    }

    @Test
    fun `a refusal is a normal result, not a tool error`() = runTest {
        // Reported through the error envelope it reads as transport noise, and
        // the model's correct response to transport noise is to retry — which
        // here is an infinite loop over a state only the user can change.
        val result = DeviceControlStartHandler(session(DeviceControlStatus.NotInstalled)).execute(noArgs)

        assertEquals("shizuku_not_installed", result["outcome"]?.jsonPrimitive?.content)
        assertFalse(result["active"]!!.jsonPrimitive.boolean)
        assertTrue(result["message"]!!.jsonPrimitive.content.contains("install", ignoreCase = true))
    }

    @Test
    fun `every arm's code reaches the wire verbatim`() = runTest {
        val codes = listOf(
            DeviceControlStatus.NotInstalled to "shizuku_not_installed",
            DeviceControlStatus.NotRunning to "shizuku_not_running",
            DeviceControlStatus.PermissionDenied to "permission_denied",
            DeviceControlStatus.Ready to "granted",
        )

        codes.forEach { (status, expected) ->
            val result = DeviceControlStartHandler(session(status)).execute(noArgs)
            assertEquals(expected, result["outcome"]?.jsonPrimitive?.content)
        }
    }

    @Test
    fun `stop distinguishes releasing from finding nothing held, and both are ok`() = runTest {
        val session = session()
        val handler = DeviceControlStopHandler(session)

        val nothingHeld = handler.execute(noArgs)
        assertFalse(nothingHeld["released"]!!.jsonPrimitive.boolean)

        session.start()
        val released = handler.execute(noArgs)
        assertTrue(released["released"]!!.jsonPrimitive.boolean)
        assertFalse(released["active"]!!.jsonPrimitive.boolean)
        assertFalse(session.isActive())
    }

    @Test
    fun `the handlers' tool ids match the catalog exactly`() {
        // A handler whose id drifts from its definition is advertised and then
        // answered with unknown_tool — silent on both sides.
        val handlerIds = setOf(
            DeviceControlStartHandler(session()).toolId,
            DeviceControlStopHandler(session()).toolId,
        )

        assertEquals(DeviceToolCatalog.LIFECYCLE_TOOL_IDS, handlerIds)
    }
}
