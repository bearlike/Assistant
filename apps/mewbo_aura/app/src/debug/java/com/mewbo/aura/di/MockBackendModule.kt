package com.mewbo.aura.di

import com.mewbo.aura.mock.MockBackendFlags
import com.mewbo.aura.mock.MockBackendFlagsImpl
import com.mewbo.aura.mock.MockBackendInterceptor
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import dagger.multibindings.IntoSet
import okhttp3.Interceptor

/**
 * Debug build-type source set's wiring for the mock backend (Gitea #181 follow-up). Two
 * responsibilities: bind the real [MockBackendFlagsImpl] to the [MockBackendFlags] seam (see that
 * interface's own KDoc for why it lives in `main/`), and contribute [MockBackendInterceptor] into
 * the `Set<Interceptor>` [DataModule.provideOkHttpClient] now consumes via
 * [dagger.multibindings.IntoSet] - release's counterpart contributes nothing to that set at all
 * (Dagger multibindings default to empty when no variant provides an `@IntoSet` entry), so
 * `provideOkHttpClient`'s own code is unchanged either way; only debug ever sees a non-empty set.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class MockBackendModule {
    @Binds
    abstract fun bindMockBackendFlags(impl: MockBackendFlagsImpl): MockBackendFlags

    companion object {
        @Provides
        @IntoSet
        fun provideMockBackendInterceptor(interceptor: MockBackendInterceptor): Interceptor = interceptor
    }
}
