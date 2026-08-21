package com.mewbo.aura.data.device

/**
 * Whether this device is a television — the predicate the assist-role surfaces hide on.
 *
 * WHY a TV hides them (no sideloaded app can hold the assistant role on any TV) is a platform fact
 * with ONE home: `apps/mewbo_aura/CLAUDE.md` § "TV-shape facts". Do not restate it here; it drifted
 * across five files once already.
 *
 * Single-method seam for the same reason as [DevicePermissionChecker] and
 * [com.mewbo.aura.data.device.shizuku.DeviceControlGate]: the consumer stays plain-JVM testable
 * with no `PackageManager` in the test.
 *
 * Declared HERE rather than beside its one consumer in `ui/settings/` because it is a platform
 * fact, not a settings fact — `data/` may not import `ui/`, so a predicate declared at the top
 * layer could not be reached by `voice/` or `data/` later without moving it.
 */
fun interface TelevisionChecker {
    /** `O(1)` — one feature lookup plus at most one binder call. Constant for a process's life. */
    fun isTelevision(): Boolean
}
