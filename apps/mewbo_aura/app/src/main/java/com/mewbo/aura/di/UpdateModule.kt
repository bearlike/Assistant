package com.mewbo.aura.di

import android.content.Context
import android.content.pm.PackageInfo
import com.mewbo.aura.BuildConfig
import com.mewbo.aura.data.update.ApkIdentity
import com.mewbo.aura.data.update.ApkInstaller
import com.mewbo.aura.data.update.PackageFacts
import com.mewbo.aura.data.update.PlatformInstaller
import com.mewbo.aura.data.update.ReleaseApi
import com.mewbo.aura.data.update.UpdateChannel
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import java.io.File
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Qualifier
import javax.inject.Singleton
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

/** Qualifies the in-app updater's OWN HTTP client and Retrofit — see [UpdateModule.provideUpdateHttpClient]. */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class UpdateHttp

/** Qualifies the directory a downloaded APK lands in. */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class UpdateDownloads

/**
 * The in-app updater's wiring.
 *
 * Everything here exists to keep the updater's traffic completely apart from the user's Mewbo
 * backend, and to keep the release SOURCE a build fact rather than a runtime one.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class UpdateModule {

    @Binds
    abstract fun bindPackageFacts(impl: AndroidPackageFacts): PackageFacts

    @Binds
    abstract fun bindPlatformInstaller(impl: ApkInstaller): PlatformInstaller

    companion object {

        /**
         * Where this build looks for releases — read off `BuildConfig`, never off settings.
         *
         * `FLAVOR` and `BUILD_TYPE` come free with the `buildConfig` feature and are the two facts
         * the asset picker matches on. Injecting the whole thing as a value, rather than letting
         * the repository read `BuildConfig` itself, is what keeps that repository a plain-JVM class
         * a test can point at a fake forge.
         */
        @Provides
        @Singleton
        fun provideUpdateChannel(): UpdateChannel = UpdateChannel(
            apiRoot = BuildConfig.UPDATE_API_ROOT,
            owner = BuildConfig.UPDATE_REPO_OWNER,
            repo = BuildConfig.UPDATE_REPO_NAME,
            flavor = BuildConfig.FLAVOR,
            buildType = BuildConfig.BUILD_TYPE,
        )

        /**
         * A BARE client, built from scratch — deliberately NOT `okHttpClient.newBuilder()`.
         *
         * **This is the one place in the app where deriving from the shared client would be
         * actively wrong, and it is worth stating because the derive is otherwise the house
         * idiom** (`SpeechModule.provideSpeechRetrofit`, `DataModule.provideEventSourceFactory`).
         * `newBuilder()` COPIES the interceptor chain, and that chain carries two things this
         * traffic must never see: `BaseUrlInterceptor`, which rewrites every request's host to the
         * user's own Mewbo server — so the release request would be sent to the wrong host
         * entirely — and `AuthInterceptor`, which attaches the user's Mewbo API key, which would
         * then be handed to a public forge on every update check. Broken and a credential leak, in
         * one line nobody would notice.
         *
         * `callTimeout(0)` because a call here is a hundred-megabyte download and the shared
         * client's 30s ceiling would abort every one of them. The read timeout is what still bounds
         * a dead socket, exactly as it does for SSE.
         *
         * Enterprise TLS needs nothing here: the deployment's root CA is a Network Security Config
         * trust anchor, which is an application-wide setting, so a client built from scratch trusts
         * it identically.
         */
        @Provides
        @Singleton
        @UpdateHttp
        fun provideUpdateHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .callTimeout(0, TimeUnit.MILLISECONDS)
            .readTimeout(UPDATE_READ_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .build()

        @Provides
        @Singleton
        @UpdateHttp
        fun provideUpdateRetrofit(@UpdateHttp client: OkHttpClient, json: Json, channel: UpdateChannel): Retrofit =
            Retrofit.Builder()
                // Unlike every other Retrofit in this app, this base URL is REAL and is dialled —
                // nothing rewrites it, because the release source is fixed at build time. The
                // fallback keeps an unconfigured build constructible; `UpdateChannel.isConfigured`
                // is what stops it ever being used.
                .baseUrl(channel.apiRoot.ifBlank { UNCONFIGURED_BASE_URL })
                .client(client)
                .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
                .build()

        @Provides
        @Singleton
        fun provideReleaseApi(@UpdateHttp retrofit: Retrofit): ReleaseApi = retrofit.create(ReleaseApi::class.java)

        /**
         * The download directory, under `cacheDir`.
         *
         * Cache rather than files: the OS may reclaim it under storage pressure, which for a
         * hundred-megabyte artifact is the behaviour we want — a download the user abandoned should
         * not hold the space forever, and losing it costs only a re-fetch. Nothing here is ever
         * installed without being re-verified first.
         */
        @Provides
        @Singleton
        @UpdateDownloads
        fun provideUpdateDownloadDir(@ApplicationContext context: Context): File =
            File(context.cacheDir, "updates")

        private const val UPDATE_READ_TIMEOUT_SECONDS = 30L

        /** Never dialled — a build with no release source reports [UpdateChannel.isConfigured]
         * false and never issues a request. Retrofit simply refuses to build without a base URL. */
        private const val UNCONFIGURED_BASE_URL = "http://localhost/"
    }
}

/**
 * The real `PackageManager` reads behind [PackageFacts].
 *
 * A separate class rather than a `@Provides` lambda because it holds two methods and a version-code
 * branch; the seam interface is what keeps the branch out of the repository's tests.
 */
@Singleton
class AndroidPackageFacts @Inject constructor(
    @ApplicationContext private val context: Context,
) : PackageFacts {

    override fun installed(): ApkIdentity {
        val info = runCatching { context.packageManager.getPackageInfo(context.packageName, 0) }.getOrNull()
        return identityOf(info) ?: ApkIdentity(context.packageName, "", 0)
    }

    override fun archive(file: File): ApkIdentity? =
        identityOf(runCatching { context.packageManager.getPackageArchiveInfo(file.absolutePath, 0) }.getOrNull())

    /** `longVersionCode` is API 28 and the floor is 30, so there is no legacy branch to keep — but
     * `versionName` is genuinely nullable on both, and a null there must not read as version "0". */
    private fun identityOf(info: PackageInfo?): ApkIdentity? = info?.let {
        ApkIdentity(
            packageName = it.packageName.orEmpty(),
            versionName = it.versionName.orEmpty(),
            versionCode = it.longVersionCode,
        )
    }
}
