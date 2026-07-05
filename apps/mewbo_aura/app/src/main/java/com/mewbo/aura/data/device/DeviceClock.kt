package com.mewbo.aura.data.device

/** Single-method seam over the wall clock, epoch seconds as a `Double` to match the wire
 * contract's `expires_at` shape - [DeviceToolExecutor] injects it so staleness checks are
 * unit-testable with a fixed fake instead of the real clock. */
fun interface DeviceClock {
    fun nowEpochSeconds(): Double
}
