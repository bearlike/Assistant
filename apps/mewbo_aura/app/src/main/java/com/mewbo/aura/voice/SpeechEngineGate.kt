package com.mewbo.aura.voice

import com.mewbo.aura.data.model.SpeechDirection
import kotlinx.coroutines.flow.Flow

/**
 * Which engine the user has chosen for each direction — a bare stored id, blank meaning on-device
 * ([com.mewbo.aura.data.model.SpeechCatalog.ON_DEVICE]).
 *
 * A `fun interface` bound in `di/` to [com.mewbo.aura.data.settings.SettingsStore]'s two flows,
 * for exactly the reason `DeviceToolGate` is one ([`di/CLAUDE.md`](../di/CLAUDE.md)): it keeps
 * [SelectedTranscriber]/[SelectedSynthesizer] constructible in a plain-JVM unit test. Injecting
 * `SettingsStore` directly would drag in the Android Keystore through `KeystoreCipher` and force
 * every routing test onto Robolectric to assert a branch that is pure.
 *
 * A **flow**, not a suspend read, so the routers observe the choice rather than sampling it — a
 * change in Settings reaches the next capture with no app restart.
 */
fun interface SpeechEngineGate {
    fun selection(direction: SpeechDirection): Flow<String>
}
