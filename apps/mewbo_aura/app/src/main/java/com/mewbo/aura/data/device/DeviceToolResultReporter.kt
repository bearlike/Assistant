package com.mewbo.aura.data.device

import com.mewbo.aura.data.api.DeviceToolResultRequest

/**
 * Single-method seam over `POST /api/sessions/{sessionId}/device_tools/{callId}/result`
 * ([com.mewbo.aura.data.api.AuraApi.postDeviceToolResult]) - [DeviceToolExecutor] depends on this
 * narrow interface rather than the full `AuraApi` (18+ endpoints) so its own tests can substitute
 * a trivial recording fake instead of a full `AuraApi` test double. Implementations must never
 * retry a failed/non-200 POST (wire contract: 403/404/409 are all terminal), so this returns
 * nothing to react to - the call is fire-and-forget from the executor's point of view.
 */
fun interface DeviceToolResultReporter {
    suspend fun report(sessionId: String, callId: String, request: DeviceToolResultRequest)
}
