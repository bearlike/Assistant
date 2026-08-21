package com.mewbo.aura.ui.settings

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.automirrored.filled.Help
import androidx.compose.material.icons.filled.RadioButtonUnchecked
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import com.mewbo.aura.data.device.shizuku.DeviceControlStatus
import com.mewbo.aura.data.update.AppUpdateState
import com.mewbo.aura.ui.theme.AuraColors

/**
 * How one settings row reports its own state.
 *
 * **Colour is never the only signal.** Every tone except [Value] carries a glyph AND a word
 * alongside its tint, because a tint alone is invisible to a colour-blind reader and can be
 * flattened entirely by a high-contrast mode. The tint is the third signal, never the first.
 *
 * [Value] is deliberately glyph-less: it reports a value the user chose (a model name, a count of
 * enabled tools), not a state the system decides, so it must not read as a status claim at all.
 */
enum class StatusTone {
    /** A plain readout of something the user set. No claim, no glyph. */
    Value,

    /** The system grants it, or a live probe answered. */
    Granted,

    /** Readable, and the answer is no. */
    Missing,

    /** Readable, and the answer is a failure the user can act on. */
    Problem,

    /**
     * Not reliably readable. Renders as unknown rather than assumed — a status indicator that
     * guesses is worse than none, because a wrong "granted" sends the user hunting for a bug in
     * Mewbo rather than a grant in Android.
     */
    Unknown,
    ;

    val glyph: ImageVector?
        get() = when (this) {
            Value -> null
            Granted -> Icons.Filled.CheckCircle
            Missing -> Icons.Filled.RadioButtonUnchecked
            Problem -> Icons.Filled.Error
            Unknown -> Icons.AutoMirrored.Filled.Help
        }

    val tint: Color
        get() = when (this) {
            Value -> AuraColors.textSecondary
            Granted -> AuraColors.accentSuccess
            Missing -> AuraColors.textSecondary
            Problem -> AuraColors.accentError
            Unknown -> AuraColors.textTertiary
        }
}

/** One glanceable state readout: a word, and the tone that words wears. */
data class StatusBadge(val label: String, val tone: StatusTone)

/**
 * Whether the stored server credentials actually reach a server.
 *
 * There is no persisted "validated" flag anywhere in the app, so this is a live answer with a
 * short life: it is only ever [Connected] because a probe returned this session. A fresh screen
 * opens on [Unchecked] and says so.
 */
sealed interface ConnectionStatus {
    val badge: StatusBadge

    /** Credentials are stored and nothing has asked the server about them yet. */
    data object Unchecked : ConnectionStatus {
        override val badge = StatusBadge("Not checked", StatusTone.Unknown)
    }

    /** No server URL saved, so there is nothing to check. */
    data object Unconfigured : ConnectionStatus {
        override val badge = StatusBadge("No server set", StatusTone.Missing)
    }

    data object Checking : ConnectionStatus {
        override val badge = StatusBadge("Checking", StatusTone.Unknown)
    }

    data class Connected(val modelCount: Int) : ConnectionStatus {
        override val badge get() = StatusBadge("Connected", StatusTone.Granted)
    }

    /**
     * The probe answered, and the answer was no.
     *
     * The badge deliberately does NOT carry [reason]: a collapsed header is a glance, and a raw
     * transport message runs long enough to crowd out the section title beside it. The reason is
     * rendered in full inside the expanded card, where there is room for it.
     */
    data class Failed(val reason: String) : ConnectionStatus {
        override val badge get() = StatusBadge("Not reachable", StatusTone.Problem)
    }
}

/**
 * Whether Mewbo holds Android's assistant role.
 *
 * [Unknown] is a real outcome, not a defensive default. `RoleManager` is the only public read of
 * this, and a device that hides the role behind its own picker leaves the app with no answer at
 * all — which the row must say rather than paper over.
 */
enum class AssistantRole {
    Active,
    Inactive,
    Unknown,
    ;

    val badge: StatusBadge
        get() = when (this) {
            Active -> StatusBadge("Active", StatusTone.Granted)
            Inactive -> StatusBadge("Not set", StatusTone.Missing)
            Unknown -> StatusBadge("Unknown", StatusTone.Unknown)
        }
}

/** A runtime permission's own readout. Android answers this one exactly, so it is never unknown. */
internal fun grantBadge(granted: Boolean): StatusBadge =
    if (granted) StatusBadge("Granted", StatusTone.Granted) else StatusBadge("Not granted", StatusTone.Missing)

/**
 * Shizuku's four-way status as a row readout.
 *
 * The row says *Shizuku* because that is the thing being granted. "Screen control: Ready" named a
 * capability and hid its cause, so a user reading it could not tell whether to install an app,
 * start a service, or approve a prompt — three states with three different actions, all spelled
 * `Off`. Note this reflects Shizuku's OWN authorization, which is a different fact from an Android
 * permission grant.
 */
internal fun shizukuBadge(status: DeviceControlStatus): StatusBadge = when (status) {
    DeviceControlStatus.Ready -> StatusBadge("Granted", StatusTone.Granted)
    DeviceControlStatus.PermissionDenied -> StatusBadge("Not granted", StatusTone.Missing)
    DeviceControlStatus.NotRunning -> StatusBadge("Shizuku not running", StatusTone.Missing)
    DeviceControlStatus.NotInstalled -> StatusBadge("Shizuku not installed", StatusTone.Missing)
}

/**
 * The updater's own state as a row readout.
 *
 * **Two collapses this mapping refuses, and they are the reason it exists.** A check that never
 * completed is [StatusTone.Problem] and says so — folding [AppUpdateState.CheckFailed] into "Up to
 * date" would state an answer nobody received, which is the wrong-green this screen exists to
 * prevent. And a newer release publishing no file for this device
 * ([AppUpdateState.NoInstallableBuild]) is neither up to date nor a failure: the forge answered, the
 * answer was "there is a newer tag and nothing here fits you", and both of the tempting readouts
 * would be false. It renders as unknown, naming the situation rather than claiming an outcome.
 *
 * An available update wears [StatusTone.Missing] rather than [StatusTone.Problem] for the same
 * reason a missing permission does: it is a readable "no, you are not on the newest", not something
 * broken. [StatusTone.Problem]'s error glyph and error tint would read as a fault in the app.
 */
internal fun updateBadge(state: AppUpdateState): StatusBadge = when (state) {
    AppUpdateState.NotChecked -> StatusBadge("Not checked", StatusTone.Unknown)
    AppUpdateState.Unsupported -> StatusBadge("Not configured", StatusTone.Unknown)
    AppUpdateState.Checking -> StatusBadge("Checking", StatusTone.Unknown)
    is AppUpdateState.UpToDate -> StatusBadge("Up to date", StatusTone.Granted)
    is AppUpdateState.Available -> StatusBadge("Update available", StatusTone.Missing)
    is AppUpdateState.NoInstallableBuild -> StatusBadge("No build for this device", StatusTone.Unknown)
    is AppUpdateState.CheckFailed -> StatusBadge("Check failed", StatusTone.Problem)
    is AppUpdateState.Downloading -> StatusBadge("Downloading", StatusTone.Unknown)
    is AppUpdateState.ReadyToInstall -> StatusBadge("Ready to install", StatusTone.Missing)
    is AppUpdateState.Installing -> StatusBadge("Installing", StatusTone.Unknown)
    is AppUpdateState.Failed -> StatusBadge("Update failed", StatusTone.Problem)
}

/**
 * Every row the System-permissions section reports, as the tones its header folds.
 *
 * The list lives here, beside [permissionSummary], rather than inline at the call site so that
 * "this row is on screen" and "this row is counted in the header" are one fact instead of two. A
 * row added to the section but forgotten here would leave the header claiming "All granted" over a
 * permission that is not — the wrong-green this screen exists to prevent, and it is silent.
 */
internal fun systemPermissionTones(state: SettingsUiState): List<StatusTone> = listOfNotNull(
    // The assistant-role row is HIDDEN on a TV (its tap opens a picker the device may not even
    // carry), so its tone must leave the count too — the row on screen and the row folded into
    // the header are one fact, and a header counting a hidden row would read wrong for the rest
    // of the screen's life.
    if (state.isTelevision) null else state.assistantRole.badge.tone,
    grantBadge(state.notificationsGranted).tone,
    grantBadge(state.smsAccessGranted).tone,
    shizukuBadge(state.deviceControlStatus).tone,
    grantBadge(state.overlayPermissionGranted).tone,
)

/**
 * The collapsed Permissions header's own readout, folded from the rows underneath it.
 *
 * It counts what IS granted and never asserts the remainder, so one unreadable row cannot turn
 * into a claim about the others. Any unknown row drags the whole summary to [StatusTone.Unknown],
 * because a header reading green over an unknown row is the same wrong claim one level up.
 */
internal fun permissionSummary(tones: List<StatusTone>): StatusBadge {
    if (tones.isEmpty()) return StatusBadge("", StatusTone.Value)
    val granted = tones.count { it == StatusTone.Granted }
    return when {
        granted == tones.size -> StatusBadge("All granted", StatusTone.Granted)
        tones.any { it == StatusTone.Unknown } -> StatusBadge("$granted of ${tones.size} granted", StatusTone.Unknown)
        else -> StatusBadge("$granted of ${tones.size} granted", StatusTone.Missing)
    }
}

/** The collapsed Device tools header. A count of the user's own switches, so it makes no state
 * claim and wears no tint. */
internal fun toolSummary(enabled: Int, total: Int): StatusBadge =
    StatusBadge("$enabled of $total on", StatusTone.Value)
