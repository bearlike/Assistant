package com.mewbo.aura.data.device

/**
 * Settings presentation of the per-tool device toggles. The GATE is tool-id-level
 * (`SettingsStore.disabledDeviceToolIds` / [DeviceToolGate]), so the persisted state and the
 * `DeviceToolCatalog`/`DeviceToolExecutor` intersection stay exact; this object is only the ordered,
 * human-labelled GROUPING the settings screen renders (the "group sensibly" half of the brief).
 *
 * Pure data, zero Compose/Android imports (data/ layer law) - the UI maps [GROUPS] to switches.
 * [DeviceToolTogglesTest] pins the one invariant that matters: every id in [DeviceToolCatalog.ALL]
 * appears here exactly once, so no shipped tool is silently un-toggleable and no dead id lingers.
 */
data class DeviceToolToggle(val toolId: String, val label: String)

data class DeviceToolToggleGroup(val title: String, val toggles: List<DeviceToolToggle>)

object DeviceToolToggles {
    val GROUPS: List<DeviceToolToggleGroup> = listOf(
        DeviceToolToggleGroup(
            title = "Time & battery",
            toggles = listOf(
                DeviceToolToggle("device_get_time", "Current time"),
                DeviceToolToggle("device_get_battery", "Battery status"),
            ),
        ),
        DeviceToolToggleGroup(
            title = "Alarms & timers",
            toggles = listOf(
                DeviceToolToggle("device_set_alarm", "Set alarms"),
                DeviceToolToggle("device_set_timer", "Set timers"),
                DeviceToolToggle("device_get_next_alarm", "Read next alarm"),
                DeviceToolToggle("device_dismiss_alarm", "Dismiss alarms"),
            ),
        ),
        DeviceToolToggleGroup(
            title = "Attention",
            toggles = listOf(
                DeviceToolToggle("device_wake", "Wake the device"),
            ),
        ),
        DeviceToolToggleGroup(
            title = "Messaging",
            toggles = listOf(
                DeviceToolToggle("device_read_latest_sms", "Read latest SMS"),
                DeviceToolToggle("device_send_sms", "Send SMS"),
            ),
        ),
        DeviceToolToggleGroup(
            title = "Screen control",
            toggles = listOf(
                DeviceToolToggle("device_ui", "See the screen"),
                DeviceToolToggle("device_action", "Tap, swipe and type"),
                DeviceToolToggle("device_shell", "Run shell commands"),
            ),
        ),
    )

    /**
     * Screen-control tools start OFF, unlike the other nine.
     *
     * Those hand a request to a system API — an alarm, a timer. These drive the
     * user's phone and read every screen they capture, so the user opts in
     * rather than out. [com.mewbo.aura.data.settings.SettingsStore] seeds its
     * disabled set from this, so a fresh install advertises none of them even
     * when Shizuku is already running.
     */
    val DEFAULT_DISABLED_TOOL_IDS: Set<String> =
        setOf("device_ui", "device_action", "device_shell")
}
