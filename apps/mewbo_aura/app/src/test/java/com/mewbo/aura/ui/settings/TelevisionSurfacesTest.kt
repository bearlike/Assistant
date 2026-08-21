package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * What a television hides, and what the header must then stop counting.
 *
 * No sideloaded app can hold the assistant role on Android TV, Google TV or Fire TV, so the
 * "Default assistant" row is removed rather than disabled there. The fold underneath it has to
 * agree: a header counting a row nobody can see reports a permission short forever, on a device
 * where nothing the user does can ever close the gap.
 *
 * Pure over [systemPermissionTones]/[permissionSummary], the same shape as [SettingsStatusTest] —
 * the claim is what the fold computes, not what renders, so no Robolectric.
 */
class TelevisionSurfacesTest {

    /** Everything Android grants, granted — so the only variable left is the hidden row. */
    private fun granted(isTelevision: Boolean) = SettingsUiState(
        assistantRole = AssistantRole.Unknown,
        notificationsGranted = true,
        smsAccessGranted = true,
        deviceControlStatus = DeviceControlStatus.Ready,
        overlayPermissionGranted = true,
        isTelevision = isTelevision,
    )

    @Test
    fun `a television drops the assistant row from the permissions count`() {
        assertEquals(5, systemPermissionTones(granted(isTelevision = false)).size)
        assertEquals(4, systemPermissionTones(granted(isTelevision = true)).size)
    }

    @Test
    fun `a television can reach All granted but a handheld with no role cannot`() {
        // The discriminating pair. On a handheld an unread role legitimately drags the header to
        // Unknown — there is a picker and the user may yet use it. On a TV that same Unknown is
        // permanent and means nothing, so counting it would leave the header claiming a shortfall
        // no action can fix. Both assertions fail the moment the hidden row is counted again.
        val handheld = permissionSummary(systemPermissionTones(granted(isTelevision = false)))
        assertEquals("4 of 5 granted", handheld.label)
        assertEquals(StatusTone.Unknown, handheld.tone)

        val television = permissionSummary(systemPermissionTones(granted(isTelevision = true)))
        assertEquals("All granted", television.label)
        assertEquals(StatusTone.Granted, television.tone)
    }

    @Test
    fun `a handheld is untouched by the flag's existence`() {
        // The default is false, so every device this screen has ever run on keeps its five rows.
        assertEquals(
            systemPermissionTones(granted(isTelevision = false)),
            systemPermissionTones(SettingsUiState(
                assistantRole = AssistantRole.Unknown,
                notificationsGranted = true,
                smsAccessGranted = true,
                deviceControlStatus = DeviceControlStatus.Ready,
                overlayPermissionGranted = true,
            )),
        )
    }
}
