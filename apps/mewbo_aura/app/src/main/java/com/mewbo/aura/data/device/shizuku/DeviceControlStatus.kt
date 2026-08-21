package com.mewbo.aura.data.device.shizuku

import kotlinx.coroutines.flow.StateFlow

/**
 * Why device control is or is not available right now.
 *
 * **A boolean would be the wrong shape.** "Off" has three causes here and they
 * need three different actions from the user: install an app, re-run a command
 * after a reboot, or grant a permission. On a non-rooted device the Shizuku
 * service does NOT survive a reboot, so [NotRunning] is a normal, recurring
 * state rather than an error — which is exactly why Settings must name it
 * instead of showing an unexplained disabled switch.
 */
sealed interface DeviceControlStatus {
    /** The Shizuku app is not installed. */
    data object NotInstalled : DeviceControlStatus

    /** Installed, but the service is not running — the after-every-reboot state. */
    data object NotRunning : DeviceControlStatus

    /** Running, but the app has not been granted access to it. */
    data object PermissionDenied : DeviceControlStatus

    /** Running and granted; the control tools are advertised. */
    data object Ready : DeviceControlStatus

    val isReady: Boolean get() = this == Ready
}

/**
 * Single-method seam over the live Shizuku state, for the same reason
 * `DevicePermissionChecker` and `DeviceToolGate` exist: it keeps
 * `DeviceToolCatalog` unit-testable on a plain JVM, with no Shizuku binder and
 * no Android framework.
 */
fun interface DeviceControlGate {
    suspend fun isReady(): Boolean
}

/**
 * Seam over [ShizukuDeviceControl.status] — the PUSHED status, not a one-shot
 * read.
 *
 * The one-shot read is why the composer could say "6 device" while Settings
 * said "Ready": authorising Shizuku changes this status with nothing in the app
 * touched, so any surface holding a snapshot keeps it until the process
 * restarts. A `StateFlow` is what lets a surface re-derive instead of
 * remembering.
 */
fun interface DeviceControlStatusSource {
    fun status(): StateFlow<DeviceControlStatus>
}

/**
 * Seam over [ShizukuDeviceControl.service] — "is the shell-UID service actually
 * bound", answered by binding it.
 *
 * Separate from [DeviceControlStatusSource] because the two answer different
 * questions: the status says Shizuku would allow a bind, this says one
 * succeeded. A grant that reports success on the first without the second is a
 * promise the first tool call breaks.
 */
fun interface DeviceControlBinder {
    suspend fun bind(): Boolean
}

/**
 * Seam over [IDeviceService.shell] — a command at shell UID, returning its
 * combined stdout+stderr, or `null` when the service could not be reached.
 *
 * **There is one shell channel and this is a view of it, not a second one.**
 * `device_shell` reaches the same `IDeviceService.shell`; this exists so a class
 * that needs one fixed, internal command does not have to inject
 * [ShizukuDeviceControl] — which would drag a `Context` and a real binder into a
 * test, the exact cost [DeviceControlStatusSource] and [DeviceControlBinder]
 * exist to avoid.
 *
 * `null` rather than a thrown exception, and rather than an empty string: an
 * unreachable service and a command that legitimately printed nothing are
 * different facts, and a caller acts differently on each.
 */
fun interface DeviceShellRunner {
    suspend fun run(command: String, timeoutMs: Int): String?
}
