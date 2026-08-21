package com.mewbo.aura.data.device

import com.mewbo.aura.data.device.shizuku.DeviceControlGate
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
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
    fun `ships exactly the defined tools, no more`() {
        val ids = DeviceToolCatalog.ALL.map { it.toolId }.toSet()
        assertEquals(
            setOf(
                "device_get_time", "device_get_battery", "device_set_alarm", "device_set_timer", "device_wake",
                "device_read_latest_sms", "device_send_sms", "device_get_next_alarm", "device_dismiss_alarm",
                // Screen control — gated on Shizuku, not on a runtime permission.
                "device_ui", "device_action", "device_shell",
                // The grant's lifecycle pair — gated on neither.
                "device_control_start", "device_control_stop",
            ),
            ids,
        )
    }

    // --- screen control: gated on the Shizuku service, not on an OS permission ---

    @Test
    fun `control tools are ABSENT when device control is not ready`() {
        // The whole failure model: when Shizuku is down the tools are simply
        // not advertised. There is no error state to render and no half-working
        // capability to explain.
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            deviceControlReady = false,
        )

        assertTrue(available.none { it.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS })
        // Every non-control tool is unaffected — the gate is per-tool.
        assertTrue(available.any { it.toolId == "device_get_battery" })
    }

    @Test
    fun `control tools appear once device control is ready`() {
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            deviceControlReady = true,
        )

        assertTrue(available.any { it.toolId == "device_ui" })
        assertTrue(available.any { it.toolId == "device_action" })
        assertTrue(available.any { it.toolId == "device_shell" })
    }

    // --- the grant's lifecycle pair: gated on the opt-in, on nothing else ---

    @Test
    fun `the lifecycle pair survives a DOWN service, because explaining that is its job`() {
        // Gate them on Shizuku and the only surface that can say WHY Shizuku is
        // unusable disappears exactly when it is needed. The three tools it
        // guards are still correctly absent.
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            deviceControlReady = false,
        )

        assertTrue(available.map { it.toolId }.containsAll(DeviceToolCatalog.LIFECYCLE_TOOL_IDS))
        assertTrue(available.none { it.toolId in DeviceToolCatalog.CONTROL_TOOL_IDS })
    }

    @Test
    fun `the lifecycle pair disappears when the user has opted out of screen control entirely`() {
        // Every control tool switched off is the user saying they do not want
        // their phone driven; offering to start doing it is pure context cost.
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            disabledToolIds = DeviceToolCatalog.CONTROL_TOOL_IDS,
            deviceControlReady = true,
        )

        assertTrue(available.none { it.toolId in DeviceToolCatalog.LIFECYCLE_TOOL_IDS })
    }

    @Test
    fun `one control tool left on is enough to keep the lifecycle pair`() {
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            disabledToolIds = setOf("device_action", "device_shell"),
            deviceControlReady = true,
        )

        assertTrue(available.map { it.toolId }.containsAll(DeviceToolCatalog.LIFECYCLE_TOOL_IDS))
    }

    @Test
    fun `the lifecycle pair is NOT part of the device_control capability predicate`() = runTest {
        // They can be on the wire with Shizuku absent. Counting them would
        // activate a playbook whose every step names a tool this session did
        // not send — the divergence DeviceControlAdvertiseTest exists for.
        val catalog = DeviceToolCatalog(
            DevicePermissionChecker { true },
            DeviceToolGate { emptySet() },
            DeviceControlGate { false },
        )

        assertTrue(catalog.availableTools().map { it.toolId }.containsAll(DeviceToolCatalog.LIFECYCLE_TOOL_IDS))
        assertFalse(catalog.advertisesDeviceControl())
    }

    @Test
    fun `a ready service does NOT override the user's per-tool toggle`() {
        // Shizuku being up says the capability EXISTS; the toggle says the user
        // wants it. The OS grant and the user's intent are separate layers and
        // neither may be inferred from the other.
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            disabledToolIds = setOf("device_shell"),
            deviceControlReady = true,
        )

        assertTrue(available.none { it.toolId == "device_shell" })
        assertTrue(available.any { it.toolId == "device_ui" })
    }

    @Test
    fun `control tools default to OFF, unlike the other nine`() {
        // They drive the phone and read every screen they capture, so the user
        // opts IN. SettingsStore falls back to exactly this set.
        assertEquals(
            DeviceToolCatalog.CONTROL_TOOL_IDS,
            DeviceToolToggles.DEFAULT_DISABLED_TOOL_IDS,
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

    /**
     * The tool description IS the model's only guide, so the paging contract has to be stated in
     * it, not merely implemented. Measured: an SMS read whose description said "the most recent
     * message(s)" and nothing about scope returned five unrelated messages for a question about a
     * 52-message conversation, and the answer built on them was confident and wrong. These
     * assertions are deliberately about the ADVERTISED contract - a handler that pages perfectly
     * while advertising none of it is the same silent failure.
     */
    @Test
    fun `the SMS read advertises the paging contract it actually enforces`() {
        val readDef = DeviceToolCatalog.ALL.first { it.toolId == "device_read_latest_sms" }
        val properties = readDef.parameters.getValue("properties").jsonObject

        val advertisedMax = properties.getValue("count").jsonObject.getValue("maximum").jsonPrimitive.int
        assertEquals(
            "a schema promising more than the handler clamps to silently returns less than asked",
            DeviceReadWindow.SMS.maxCount,
            advertisedMax,
        )
        assertTrue("without `offset` in the schema the handler's paging is unreachable", properties.containsKey("offset"))
        assertEquals(0, properties.getValue("offset").jsonObject.getValue("minimum").jsonPrimitive.int)
        assertTrue("`sender_filter` is how one conversation is reached", properties.containsKey("sender_filter"))

        assertTrue(
            "the description must tell the model that a partial page is signalled by has_more",
            readDef.description.contains("has_more"),
        )
        assertTrue(
            "the description must resolve whether `count` is per-conversation or mailbox-wide",
            readDef.description.contains("PAGE SIZE"),
        )
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
        val catalog = DeviceToolCatalog(
            DevicePermissionChecker { true },
            DeviceToolGate { emptySet() },
            DeviceControlGate { true },
        )

        assertFalse(catalog.availableTools().isEmpty())
        assertEquals(DeviceToolCatalog.ALL.size, catalog.availableTools().size)
    }

    // --- per-tool settings toggle intersected at the SAME seam ---

    @Test
    fun `a disabled tool is dropped from filterAvailable even when its permission is granted`() {
        // Device control is READY here, so the only thing removing tools is the
        // toggle under test — otherwise this asserts against two gates at once
        // and stops being a test of the toggle.
        val available = DeviceToolCatalog.filterAvailable(
            DeviceToolCatalog.ALL,
            DevicePermissionChecker { true },
            disabledToolIds = setOf("device_get_time", "device_send_sms"),
            deviceControlReady = true,
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
            DeviceToolCatalog.filterAvailable(DeviceToolCatalog.ALL, granted, deviceControlReady = true),
            DeviceToolCatalog.filterAvailable(
                DeviceToolCatalog.ALL,
                granted,
                disabledToolIds = emptySet(),
                deviceControlReady = true,
            ),
        )
    }

    @Test
    fun `availableTools omits a tool the gate reports disabled`() = runTest {
        val catalog = DeviceToolCatalog(
            DevicePermissionChecker { true },
            DeviceToolGate { setOf("device_set_alarm") },
            DeviceControlGate { true },
        )

        val ids = catalog.availableTools().map { it.toolId }
        assertFalse("device_set_alarm" in ids)
        assertTrue("device_set_timer" in ids)
    }
}
