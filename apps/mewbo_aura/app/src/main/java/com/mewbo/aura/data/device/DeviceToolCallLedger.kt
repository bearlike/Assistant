package com.mewbo.aura.data.device

/**
 * Dedup seam [DeviceToolExecutor] checks every `device_tool_call` against before executing it.
 * [SessionStreamClient][com.mewbo.aura.data.sse.SessionStreamClient]'s reconnect-replays-the-
 * full-backlog design (data/CLAUDE.md) means every reconnect re-delivers every device_tool_call
 * event ever sent on the session - without this, a reconnect would re-fire an alarm/timer/wake.
 * An interface (not the concrete [DeviceToolCallHistory]) so executor tests can substitute an
 * in-memory fake instead of needing a real Android `Context`/DataStore (no Robolectric in this
 * module, apps/mewbo_aura/CLAUDE.md).
 */
interface DeviceToolCallLedger {
    /** Atomically checks-and-records in one step: `true` only the FIRST time [callId] is seen
     * (caller should execute it then); `false` on every later replay/duplicate. */
    suspend fun recordIfNew(callId: String): Boolean
}
