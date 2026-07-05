package com.mewbo.aura.voice

import com.mewbo.aura.data.device.DevicePermissionChecker
import com.mewbo.aura.data.repo.RunRepository
import com.mewbo.aura.data.repo.SessionRepository
import com.mewbo.aura.data.settings.SettingsStore
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

/**
 * [AuraSession] is an `android.service.voice.VoiceInteractionSession`, not an Android entry point
 * Hilt can `@AndroidEntryPoint`-inject into directly - this is the sanctioned Hilt escape hatch
 * (`EntryPointAccessors.fromApplication`) for reaching EXISTING singleton bindings from a plain
 * framework class Hilt doesn't manage. Not a `@Module`/`@Provides` - it exposes accessors for
 * bindings the app's real modules (`DataModule`, `VoiceModule`) already provide.
 */
@EntryPoint
@InstallIn(SingletonComponent::class)
interface AssistEntryPoint {
    fun transcriber(): Transcriber
    fun synthesizer(): Synthesizer
    fun sessionRepository(): SessionRepository
    fun runRepository(): RunRepository
    fun settingsStore(): SettingsStore
    fun haptics(): AuraHaptics

    /** Gitea #180 P5's mic-tap gate reuses `DeviceModule`'s existing `Context.checkSelfPermission`
     * seam (#179) rather than a second one - `RECORD_AUDIO` is checked the same way `device_*`
     * tools already check theirs. */
    fun devicePermissionChecker(): DevicePermissionChecker
}
