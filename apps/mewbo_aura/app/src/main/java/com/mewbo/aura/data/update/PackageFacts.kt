package com.mewbo.aura.data.update

import java.io.File

/** What an APK says it is — the same three fields whether it is installed or sitting on disk. */
data class ApkIdentity(val packageName: String, val versionName: String, val versionCode: Long) {
    /** The comparable form, or `null` when the name carries no numeric version at all. */
    val version: AppVersion? get() = AppVersion.parse(versionName)
}

/**
 * The two `PackageManager` reads the updater needs, as one narrow seam.
 *
 * Declared here and bound in `di/UpdateModule` for the reason the whole `data/` layer already
 * follows for `DevicePermissionChecker` and friends: it keeps [AppUpdateRepository] — which owns
 * the version arithmetic, the asset choice and the verification rules worth testing — constructible
 * in a plain-JVM test, with no `Context` and no Robolectric.
 *
 * One interface with two methods rather than two seams, because they are one question asked of two
 * subjects: *what is this APK*. The comparison between the two answers is what "is this an update"
 * means.
 */
interface PackageFacts {

    /** The running app. `O(1)`. */
    fun installed(): ApkIdentity

    /**
     * A downloaded APK's own manifest, or `null` when the file is not a readable package.
     *
     * `null` is a real answer, not an error to swallow: a truncated or wrong-typed download parses
     * as nothing, and that has to reach the user as a refusal rather than as a handoff to the
     * installer. `O(one file)`.
     */
    fun archive(file: File): ApkIdentity?
}
