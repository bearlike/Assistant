package com.mewbo.aura.data.update

import java.io.File

/**
 * Handing an APK to the platform, as a seam.
 *
 * [ApkInstaller] is the one implementation and is bound to this in `di/UpdateModule`. The interface
 * exists for the same reason [PackageFacts] does — it is what keeps [AppUpdateRepository] a
 * plain-JVM class, so the lifecycle it owns (check → download → verify → install) can be driven end
 * to end in a unit test with no `Context`, no `PackageInstaller` and no device.
 */
interface PlatformInstaller {

    /** Whether the OS will let this app install packages ("Install unknown apps"). `O(1)`. */
    fun canInstallPackages(): Boolean

    /** Open the system screen that grants it. `false` when no such screen resolves on this device,
     * so the caller can say so rather than leave a tap that does nothing. */
    fun openInstallPermissionScreen(): Boolean

    /** Suspend until the platform reports a terminal status for [apk]. */
    suspend fun install(apk: File): InstallOutcome
}
