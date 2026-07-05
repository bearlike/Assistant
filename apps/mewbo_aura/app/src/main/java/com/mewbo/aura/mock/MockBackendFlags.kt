package com.mewbo.aura.mock

import kotlinx.coroutines.flow.Flow

/**
 * The debug/release-swapped seam for the mock backend toggle (same pattern as `voice/`'s
 * `Transcriber`/`Synthesizer`: an interface here in `main/` so `ui/settings/` and [MainActivity][com.mewbo.aura.MainActivity]
 * can inject and reference it uniformly, with the REAL DataStore-backed implementation living only
 * in `app/src/debug/.../mock/MockBackendFlagsImpl.kt` and a permanently-off no-op bound in
 * `app/src/release/.../di/MockBackendModule.kt` — release never sees the debug implementation on
 * its classpath, mirroring `VoiceModule`'s own release counterpart.
 *
 * Deliberately NOT added to [com.mewbo.aura.data.settings.SettingsStore] (`data/` is off-limits for
 * this concern, user directive) — this is its own small, isolated seam so a release build's `data/`
 * layer never carries any concept of a mock backend at all.
 */
interface MockBackendFlags {
    val enabled: Flow<Boolean>
    suspend fun setEnabled(value: Boolean)

    /** Synchronous snapshot for [com.mewbo.aura.mock.MockBackendInterceptor] (debug-only), which
     * runs on OkHttp's own interceptor chain and cannot suspend - mirrors the same
     * `runBlocking { flow.first() }` shape [com.mewbo.aura.di.AuthInterceptor]/[com.mewbo.aura.di.BaseUrlInterceptor]
     * already use to read [com.mewbo.aura.data.settings.SettingsStore] synchronously from the same
     * chain. A no-op release implementation just returns `false` with no DataStore read at all. */
    fun isEnabledBlocking(): Boolean
}
