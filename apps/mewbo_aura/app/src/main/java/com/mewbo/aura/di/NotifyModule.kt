package com.mewbo.aura.di

import com.mewbo.aura.data.repo.RunNotifications
import com.mewbo.aura.notify.RunNotificationLauncher
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent

/**
 * Wires the turn-completion notification feature (`notify/`) to the data-layer seam. `RunRepository`
 * depends only on the narrow [RunNotifications] `fun interface` (defined in its own package, so the
 * dependency flows DOWN, never up into `notify/`/`MainActivity`); the concrete
 * [RunNotificationLauncher] that starts the foreground service is bound here — the same
 * seam-and-binding shape `DeviceModule` uses for `DeviceToolDispatch`. [RunNotifier],
 * [com.mewbo.aura.notify.RunNotificationController], and [RunNotificationLauncher] are all
 * `@Inject @Singleton`, so no explicit `@Provides` is needed beyond this one binding.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class NotifyModule {

    @Binds
    abstract fun bindRunNotifications(impl: RunNotificationLauncher): RunNotifications
}
