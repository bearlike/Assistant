package com.mewbo.aura.ui.settings

import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.update.AppUpdateState
import com.mewbo.aura.data.update.AvailableUpdate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The honesty rules for what Settings claims about state.
 *
 * These assert on the WORD and the TONE together, because that pairing is the contract: a tone
 * carries a tint and a glyph, and a tint with no word beside it is exactly the failure the design
 * forbids.
 */
class SettingsStatusTest {

    @Test
    fun `every tone that carries a tint also carries a glyph`() {
        // Colour is never the only signal. Value is the one exception, and deliberately so: it
        // reports something the user chose rather than a state, so it must not read as a claim.
        StatusTone.entries.filter { it != StatusTone.Value }.forEach { tone ->
            assertNotNull("$tone must render a glyph, not a tint alone", tone.glyph)
        }
        assertNull(StatusTone.Value.glyph)
    }

    @Test
    fun `a permission summary never asserts anything about a row it could not read`() {
        // Three granted, one unknown. "3 of 4 granted" is true; "1 not granted" would not be.
        val summary = permissionSummary(
            listOf(StatusTone.Granted, StatusTone.Granted, StatusTone.Granted, StatusTone.Unknown),
        )
        assertEquals("3 of 4 granted", summary.label)
        assertEquals(StatusTone.Unknown, summary.tone)
    }

    @Test
    fun `one unknown row drags the whole summary to unknown`() {
        // A green header over an unknown row is the same wrong claim, one level up.
        val summary = permissionSummary(listOf(StatusTone.Granted, StatusTone.Unknown))
        assertEquals(StatusTone.Unknown, summary.tone)
    }

    @Test
    fun `all granted reads as granted, and nothing less does`() {
        assertEquals(
            StatusTone.Granted,
            permissionSummary(listOf(StatusTone.Granted, StatusTone.Granted)).tone,
        )
        assertEquals(
            StatusTone.Missing,
            permissionSummary(listOf(StatusTone.Granted, StatusTone.Missing)).tone,
        )
    }

    @Test
    fun `an empty permission list claims nothing at all`() {
        val summary = permissionSummary(emptyList())
        assertEquals("", summary.label)
        assertEquals(StatusTone.Value, summary.tone)
    }

    @Test
    fun `Shizuku's three off states stay distinguishable, because each needs a different action`() {
        // A boolean would spell all three "Off" and leave the user with no way to tell installing
        // an app from restarting a service from approving a prompt.
        val labels = listOf(
            DeviceControlStatus.PermissionDenied,
            DeviceControlStatus.NotRunning,
            DeviceControlStatus.NotInstalled,
        ).map { shizukuBadge(it).label }
        assertEquals(labels.size, labels.toSet().size)
        assertTrue(labels.none { it == shizukuBadge(DeviceControlStatus.Ready).label })
        assertEquals(StatusTone.Granted, shizukuBadge(DeviceControlStatus.Ready).tone)
    }

    @Test
    fun `an unreadable assistant role says unknown, never not set`() {
        assertEquals("Unknown", AssistantRole.Unknown.badge.label)
        assertEquals(StatusTone.Unknown, AssistantRole.Unknown.badge.tone)
        assertEquals(StatusTone.Granted, AssistantRole.Active.badge.tone)
        assertEquals(StatusTone.Missing, AssistantRole.Inactive.badge.tone)
    }

    @Test
    fun `stored credentials alone are never reported as connected`() {
        // Presence is not validity: only a probe's own answer may wear the granted tone.
        assertEquals(StatusTone.Unknown, ConnectionStatus.Unchecked.badge.tone)
        assertEquals(StatusTone.Unknown, ConnectionStatus.Checking.badge.tone)
        assertEquals(StatusTone.Missing, ConnectionStatus.Unconfigured.badge.tone)
        assertEquals(StatusTone.Granted, ConnectionStatus.Connected(modelCount = 4).badge.tone)
    }

    @Test
    fun `a failed probe keeps its reason off the badge and on the state`() {
        // A collapsed header is a glance, and a raw transport message runs long enough to squeeze
        // the section title beside it down to one character per line — which shipped once. The
        // badge stays a fixed short word; the reason travels on the state for the expanded card.
        val status = ConnectionStatus.Failed(
            "CLEARTEXT communication to api not permitted by network security policy",
        )
        assertEquals("Not reachable", status.badge.label)
        assertEquals(StatusTone.Problem, status.badge.tone)
        assertTrue("the reason must survive for the expanded card", status.reason.startsWith("CLEARTEXT"))
    }

    @Test
    fun `a device-tool count makes no state claim`() {
        val summary = toolSummary(enabled = 8, total = 11)
        assertEquals("8 of 11 on", summary.label)
        assertEquals(StatusTone.Value, summary.tone)
    }

    @Test
    fun `a runtime permission is answered exactly, so it is never unknown`() {
        assertEquals(StatusTone.Granted, grantBadge(granted = true).tone)
        assertEquals(StatusTone.Missing, grantBadge(granted = false).tone)
    }

    @Test
    fun `the overlay permission is one of the rows the header counts`() {
        // The whole point of folding the row set into one function: a row on screen but missing
        // from the summary leaves the header claiming "All granted" over a permission that is not.
        // Everything granted EXCEPT the overlay must not read as all-granted.
        val allButOverlay = SettingsUiState(
            assistantRole = AssistantRole.Active,
            notificationsGranted = true,
            smsAccessGranted = true,
            deviceControlStatus = DeviceControlStatus.Ready,
            overlayPermissionGranted = false,
        )
        val summary = permissionSummary(systemPermissionTones(allButOverlay))
        assertEquals("4 of 5 granted", summary.label)
        assertEquals(StatusTone.Missing, summary.tone)
    }

    @Test
    fun `granting the overlay is what completes the permissions header`() {
        val everything = SettingsUiState(
            assistantRole = AssistantRole.Active,
            notificationsGranted = true,
            smsAccessGranted = true,
            deviceControlStatus = DeviceControlStatus.Ready,
            overlayPermissionGranted = true,
        )
        val summary = permissionSummary(systemPermissionTones(everything))
        assertEquals("All granted", summary.label)
        assertEquals(StatusTone.Granted, summary.tone)
    }

    @Test
    fun `a fresh install reports the overlay as not granted, never as unknown`() {
        // canDrawOverlays is an exact synchronous read, so this row has no honest Unknown state —
        // and a default-state screen must say "Not granted" rather than stay silent about the one
        // permission whose absence is otherwise completely invisible.
        val fresh = SettingsUiState()
        assertEquals("Not granted", grantBadge(fresh.overlayPermissionGranted).label)
        assertEquals(StatusTone.Missing, grantBadge(fresh.overlayPermissionGranted).tone)
    }

    // The updater's badge mapping, pinned here beside the permission ones because it is the same
    // contract and the same failure mode: a table of state → word → tone drifts silently, and the
    // three collapses below are each a claim nobody measured.

    @Test
    fun `a check that failed never reads as up to date`() {
        // An unreachable forge means NOBODY ASKED. Reporting that as "Up to date" states an answer
        // that was never received — the wrong-green this screen exists to prevent.
        val failed = updateBadge(AppUpdateState.CheckFailed("Unable to resolve host"))
        assertEquals("Check failed", failed.label)
        assertEquals(StatusTone.Problem, failed.tone)
        // The control: the same mapping DOES report up to date when the forge actually answered so.
        assertEquals(StatusTone.Granted, updateBadge(AppUpdateState.UpToDate("0.0.21")).tone)
    }

    @Test
    fun `a newer release with no file for this device is neither up to date nor a failure`() {
        // The measured state of the public mirror: releases carrying no APK asset at all. Both
        // tempting readouts are false, so the badge names the situation instead of claiming an
        // outcome.
        val none = updateBadge(AppUpdateState.NoInstallableBuild("aura-0.0.21.0"))
        assertEquals("No build for this device", none.label)
        assertEquals(StatusTone.Unknown, none.tone)
    }

    @Test
    fun `a build with no release source says so rather than claiming to be current`() {
        val unsupported = updateBadge(AppUpdateState.Unsupported)
        assertEquals("Not configured", unsupported.label)
        assertEquals(StatusTone.Unknown, unsupported.tone)
    }

    @Test
    fun `an available update is a readable no, not an error`() {
        // Missing, never Problem: nothing is broken. Problem's glyph is an error glyph in the error
        // tint, which would read as a fault in the app rather than as a build being available.
        val available = updateBadge(AppUpdateState.Available(anUpdate))
        assertEquals("Update available", available.label)
        assertEquals(StatusTone.Missing, available.tone)
    }

    @Test
    fun `every update state renders a word and a tone`() {
        // The mapping is exhaustive by compiler, but an arm could still ship a blank label — which
        // renders as a tint with no word beside it, the one thing StatusBadgeText must never do.
        val states = listOf(
            AppUpdateState.NotChecked,
            AppUpdateState.Unsupported,
            AppUpdateState.Checking,
            AppUpdateState.UpToDate("0.0.21"),
            AppUpdateState.NoInstallableBuild("aura-0.0.22.0"),
            AppUpdateState.CheckFailed("boom"),
            AppUpdateState.Available(anUpdate),
            AppUpdateState.Downloading(anUpdate, 1, 2),
            AppUpdateState.ReadyToInstall(anUpdate, "/tmp/x.apk"),
            AppUpdateState.Installing(anUpdate),
            AppUpdateState.Failed(anUpdate, "boom"),
        )
        states.forEach { state ->
            assertTrue("$state renders a blank badge label", updateBadge(state).label.isNotBlank())
        }
    }

    private companion object {
        val anUpdate = AvailableUpdate(
            versionLabel = "0.0.22.0",
            tagName = "aura-0.0.22.0",
            title = null,
            assetName = "aura-0.0.22-enterprise-debug.apk",
            downloadUrl = "https://example.invalid/aura.apk",
            sizeBytes = 1,
            prerelease = false,
        )
    }
}
