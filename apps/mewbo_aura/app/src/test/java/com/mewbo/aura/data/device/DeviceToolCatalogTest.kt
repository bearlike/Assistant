package com.mewbo.aura.data.device

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.buildJsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceToolCatalogTest {

    @Test
    fun `every shipped definition's tool_id matches the wire contract's regex`() {
        val regex = Regex("^device_[a-z0-9_]{1,48}$")
        DeviceToolCatalog.ALL.forEach { def -> assertTrue("${def.toolId} must match $regex", regex.matches(def.toolId)) }
    }

    @Test
    fun `constructing a definition with a bad tool_id throws`() {
        try {
            DeviceToolDefinition(toolId = "not_device_prefixed", description = "x", parameters = buildJsonObject {})
            org.junit.Assert.fail("expected IllegalArgumentException")
        } catch (e: IllegalArgumentException) {
            // expected
        }
    }

    @Test
    fun `ships exactly the nine tools defined through Phase 4, no more`() {
        val ids = DeviceToolCatalog.ALL.map { it.toolId }.toSet()
        assertEquals(
            setOf(
                "device_get_time", "device_get_battery", "device_set_alarm", "device_set_timer", "device_wake",
                "device_read_latest_sms", "device_send_sms", "device_get_next_alarm", "device_dismiss_alarm",
            ),
            ids,
        )
    }

    @Test
    fun `the two SMS tools are gated on READ_SMS-SEND_SMS respectively, not null`() {
        val smsDefs = DeviceToolCatalog.ALL.filter { it.toolId in setOf("device_read_latest_sms", "device_send_sms") }
        assertEquals(2, smsDefs.size)
        val readDef = smsDefs.first { it.toolId == "device_read_latest_sms" }
        val sendDef = smsDefs.first { it.toolId == "device_send_sms" }
        assertEquals("android.permission.READ_SMS", readDef.requiredPermission)
        assertEquals("android.permission.SEND_SMS", sendDef.requiredPermission)
    }

    @Test
    fun `SMS tools are absent from availableTools when neither permission is granted`() {
        val available = DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, DevicePermissionChecker { false })

        assertTrue(available.none { it.toolId == "device_read_latest_sms" })
        assertTrue(available.none { it.toolId == "device_send_sms" })
    }

    @Test
    fun `each SMS tool is present once its OWN permission is granted, independent of the other`() {
        val onlyReadGranted = DevicePermissionChecker { it == "android.permission.READ_SMS" }
        val availableReadOnly = DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, onlyReadGranted)
        assertTrue(availableReadOnly.any { it.toolId == "device_read_latest_sms" })
        assertTrue(availableReadOnly.none { it.toolId == "device_send_sms" })

        val bothGranted = DevicePermissionChecker { true }
        val availableBoth = DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, bothGranted)
        assertTrue(availableBoth.any { it.toolId == "device_read_latest_sms" })
        assertTrue(availableBoth.any { it.toolId == "device_send_sms" })
    }

    @Test
    fun `alarm tools carry no required permission and are always available`() {
        val alarmDefs = DeviceToolCatalog.ALL.filter { it.toolId in setOf("device_get_next_alarm", "device_dismiss_alarm") }
        assertEquals(2, alarmDefs.size)
        assertTrue(alarmDefs.all { it.requiredPermission == null })

        val available = DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, DevicePermissionChecker { false })
        assertTrue(available.any { it.toolId == "device_get_next_alarm" })
        assertTrue(available.any { it.toolId == "device_dismiss_alarm" })
    }

    @Test
    fun `a definition with no required permission is always available`() {
        val def = DeviceToolDefinition(toolId = "device_no_perm", description = "x", parameters = buildJsonObject {}, requiredPermission = null)
        val checker = DevicePermissionChecker { false } // never grants anything

        val available = DeviceToolCatalog.filterAvailable(listOf(def), checker)

        assertEquals(listOf(def), available)
    }

    @Test
    fun `a definition gated on a permission is dropped when the checker denies it`() {
        val def = DeviceToolDefinition(toolId = "device_gated", description = "x", parameters = buildJsonObject {}, requiredPermission = "android.permission.SEND_SMS")
        val checker = DevicePermissionChecker { granted -> granted != "android.permission.SEND_SMS" }

        val available = DeviceToolCatalog.filterAvailable(listOf(def), checker)

        assertTrue(available.isEmpty())
    }

    @Test
    fun `a definition gated on a permission is kept when the checker grants it`() {
        val def = DeviceToolDefinition(toolId = "device_gated", description = "x", parameters = buildJsonObject {}, requiredPermission = "android.permission.SEND_SMS")
        val checker = DevicePermissionChecker { true }

        val available = DeviceToolCatalog.filterAvailable(listOf(def), checker)

        assertEquals(listOf(def), available)
    }

    @Test
    fun `availableTools delegates to the injected checker over the static ALL list`() = runTest {
        val catalog = DeviceToolCatalog(DevicePermissionChecker { true }, DeviceToolGate { emptySet() })

        assertFalse(catalog.availableTools().isEmpty())
        assertEquals(DeviceToolCatalog.ALL.size, catalog.availableTools().size)
    }

    // --- per-tool settings toggle intersected at the SAME seam ---

    @Test
    fun `a disabled tool is dropped from filterAvailable even when its permission is granted`() {
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            disabledToolIds = setOf("device_get_time", "device_send_sms"),
        )

        assertTrue(available.none { it.toolId == "device_get_time" })
        assertTrue(available.none { it.toolId == "device_send_sms" })
        // Every OTHER tool is still present - the toggle is per-tool, not all-or-nothing.
        assertTrue(available.any { it.toolId == "device_get_battery" })
        assertEquals(DeviceToolCatalog.ALL.size - 2, available.size)
    }

    @Test
    fun `an empty disabled set leaves the permission-only result unchanged`() {
        val granted = DevicePermissionChecker { true }
        assertEquals(
            DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, granted),
            DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, granted, disabledToolIds = emptySet()),
        )
    }

    @Test
    fun `availableTools omits a tool the gate reports disabled`() = runTest {
        val catalog = DeviceToolCatalog(
            DevicePermissionChecker { true },
            DeviceToolGate { setOf("device_set_alarm") },
        )

        val ids = catalog.availableTools().map { it.toolId }
        assertFalse("device_set_alarm" in ids)
        assertTrue("device_set_timer" in ids)
    }
}
