package com.mewbo.aura.di

import com.mewbo.aura.mock.MockBackendFlags
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import dagger.multibindings.Multibinds
import javax.inject.Inject
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import okhttp3.Interceptor

/**
 * Release build-type source set's wiring: [MockBackendFlags] resolves to a permanently-OFF no-op
 * (no DataStore read/write, ever) - `com.mewbo.aura.mock.MockBackendInterceptor` isn't referenced
 * anywhere here, so it (and every other debug-only `mock/` class) is never on the release
 * classpath at all. Mirrors `di/VoiceModule.kt`'s own release counterpart exactly: this file lives
 * ONLY in the release source set, and the two `MockBackendModule`s never compile together.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class MockBackendModule {
    @Binds
    abstract fun bindMockBackendFlags(impl: NoOpMockBackendFlags): MockBackendFlags

    /** Declares the `Set<Interceptor>` multibinding as validly EMPTY in release —
     * `DataModule.provideOkHttpClient`'s `debugOnlyInterceptors` param needs the set to EXIST in
     * every variant; only the debug source set ever contributes members. Without this, release
     * Hilt compilation fails with Dagger/MissingBinding (caught at the release-assemble gate). */
    @Multibinds
    abstract fun debugOnlyInterceptors(): Set<Interceptor>
}

class NoOpMockBackendFlags @Inject constructor() : MockBackendFlags {
    override val enabled: Flow<Boolean> = flowOf(false)
    override suspend fun setEnabled(value: Boolean) = Unit
    override fun isEnabledBlocking(): Boolean = false
}
