package com.mewbo.aura.di

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.settings.SettingsStore
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Qualifier
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.Json
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.sse.EventSource
import okhttp3.sse.EventSources
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

/**
 * Retrofit is built ONCE against a placeholder base URL; [BaseUrlInterceptor] rewrites every
 * request's scheme/host/port to the CURRENT [SettingsStore.baseUrl] value before it's dispatched.
 * This is the simplest correct way to support a user-editable server URL without needing to
 * rebuild/invalidate the Retrofit/OkHttpClient singletons whenever Settings changes - one
 * interceptor, no rebuild plumbing. [SessionStreamClient][com.mewbo.aura.data.sse.SessionStreamClient]
 * builds its request URL directly from [SettingsStore.baseUrl] instead (it isn't a Retrofit call),
 * so this interceptor is a harmless no-op rewrite for it.
 */
@Module
@InstallIn(SingletonComponent::class)
object DataModule {

    private const val PLACEHOLDER_BASE_URL = "http://localhost/"

    @Provides
    @Singleton
    fun provideJson(): Json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
    }

    /**
     * [debugOnlyInterceptors] is a Dagger `Set<Interceptor>` multibinding - empty in every release
     * build (nothing in the release source set contributes to it) and, in debug, carries the mock
     * backend's [com.mewbo.aura.mock.MockBackendInterceptor][com.mewbo.aura.di.MockBackendModule]
     * (own KDoc there for why an app-wide `Set<Interceptor>` seam, not a direct dependency here, is
     * how a debug-only interceptor reaches this otherwise variant-agnostic module). Added LAST so a
     * debug interceptor that short-circuits sees the request AFTER [baseUrlInterceptor]/
     * [authInterceptor] have already run - it observes the exact same final request shape the real
     * network would have.
     */
    @Provides
    @Singleton
    fun provideOkHttpClient(
        authInterceptor: AuthInterceptor,
        baseUrlInterceptor: BaseUrlInterceptor,
        debugOnlyInterceptors: Set<@JvmSuppressWildcards Interceptor>,
    ): OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(baseUrlInterceptor)
        .addInterceptor(authInterceptor)
        .apply { debugOnlyInterceptors.forEach(::addInterceptor) }
        .callTimeout(30, TimeUnit.SECONDS)
        .build()

    /** SSE connections are long-lived and rely on 15s server heartbeats, not client read timeouts. */
    @Provides
    @Singleton
    fun provideEventSourceFactory(okHttpClient: OkHttpClient): EventSource.Factory =
        EventSources.createFactory(
            okHttpClient.newBuilder()
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .callTimeout(0, TimeUnit.MILLISECONDS)
                .build(),
        )

    @Provides
    @Singleton
    fun provideRetrofit(okHttpClient: OkHttpClient, json: Json): Retrofit = Retrofit.Builder()
        .baseUrl(PLACEHOLDER_BASE_URL)
        .client(okHttpClient)
        .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
        .build()

    @Provides
    @Singleton
    fun provideAuraApi(retrofit: Retrofit): AuraApi = retrofit.create(AuraApi::class.java)

    /** App-process-lifetime scope backing [com.mewbo.aura.data.repo.RunRepository]'s per-session
     * `shareIn` multicast and [com.mewbo.aura.data.device.DeviceToolExecutor]'s dispatch job - both
     * need a scope that outlives any single collector (a `ChatViewModel.stop()` or navigation
     * event must not tear down device-tool servicing for a still-live backend run). `SupervisorJob`
     * so one session's stream failure can't cancel a sibling's. */
    @Provides
    @Singleton
    @ApplicationScope
    fun provideApplicationScope(): CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
}

/** Qualifies the app-process-lifetime [CoroutineScope] from [DataModule.provideApplicationScope]. */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class ApplicationScope

/**
 * `X-API-Key` header auth (`api_key` query param is reserved for SSE, which can't rely on this
 * interceptor - see [com.mewbo.aura.data.sse.SessionStreamClient]).
 *
 * **`X-Mewbo-Capabilities`** (`apps` added by the Mewbo Apps design spec §2.4/§4D;
 * `ask_user` added by the ask-user-question tool) — the comma-separated capability list the
 * backend parses into `context.client_capabilities` (verified against the same
 * `request.headers.get("X-Mewbo-Capabilities", "")` split used for `stlite`). THREE capabilities ride
 * this ONE header now, gated independently:
 * - **`apps`** sent UNCONDITIONALLY — Mewbo Apps is a permanent nav surface (the drawer's "Apps" row),
 *   not an opt-in chat feature like widgets, so it carries no settings toggle of its own (spec §4D
 *   lists no such flag). Per the two-surface capability-gating law (`feedback_capability_two_surface_gating`
 *   memory / spec §2.4 "gated by client capability `apps`"), this is the advertise half; the answer
 *   half is [com.mewbo.aura.data.repo.AppRepository] + the `ui/apps/` screens rendering whatever the
 *   `/api/apps` surface returns - never advertising a capability the app won't service.
 * - **`ask_user`** sent UNCONDITIONALLY. Aura always renders the question card
 *   ([com.mewbo.aura.data.model.ChatItem.Question]) and POSTs the answer
 *   ([com.mewbo.aura.data.repo.RunRepository.answerQuestion]), so it can always service a blocked
 *   `ask_user_question` tool call — the "advertise AND answer at the SAME seam" law (root CLAUDE.md).
 *   Without it the backend never binds the tool and the agent proceeds on its own judgment.
 * - **`stlite`** sent ONLY while [SettingsStore.streamlitWidgetsEnabled] is on - unlocks the
 *   `widget_builder` plugin's `widget_ready` events ([com.mewbo.aura.ui.chat.widget.WidgetCard]); the
 *   render-half (reducer + WebView card) is gated on the SAME flag, so the app never advertises a
 *   capability it won't service.
 *
 * Reading the flag here (blocking `first()`, the same way [apiKey] is already read - DataStore
 * caches it in memory after the first read, and this runs off the main thread on OkHttp's
 * dispatcher) keeps the whole gate at ONE seam rather than per-request.
 */
class AuthInterceptor @Inject constructor(private val settingsStore: SettingsStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val apiKey = runBlocking { settingsStore.apiKey.first() }
        val widgetsEnabled = runBlocking { settingsStore.streamlitWidgetsEnabled.first() }
        // `apps` and `ask_user` are always advertised; `stlite` only while the render-half is
        // enabled. The backend splits this header on commas into `context.client_capabilities`.
        val capabilities = buildList {
            add(APPS_CAPABILITY_ID)
            add(ASK_USER_CAPABILITY_ID)
            if (widgetsEnabled) add(WIDGET_CAPABILITY_ID)
        }.joinToString(",")
        val request = chain.request().newBuilder()
            .header("X-Mewbo-Surface", "android")
            .header("X-Mewbo-Capabilities", capabilities)
            .apply {
                if (!apiKey.isNullOrBlank()) header("X-API-Key", apiKey)
            }
            .build()
        return chain.proceed(request)
    }

    private companion object {
        /** Matches the `widget_builder` plugin's `requires-capabilities` (hardcoded server-side) and
         * the console's own `WIDGET_CAPABILITY_ID` - there is no capability-discovery endpoint. */
        const val WIDGET_CAPABILITY_ID = "stlite"

        /** Matches the `app_builder` plugin's `requires-capabilities: ["apps"]` (design spec §4B)
         * and the console's own apps capability id (spec §4C `realClient.ts` advertise). */
        const val APPS_CAPABILITY_ID = "apps"

        /** Matches core's `ASK_USER_CAPABILITY` (`mewbo_core.ask_user`) — the client's promise that a
         * human can be asked to answer a blocking `ask_user_question` tool call. */
        const val ASK_USER_CAPABILITY_ID = "ask_user"
    }
}

class BaseUrlInterceptor @Inject constructor(private val settingsStore: SettingsStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val configured = runBlocking { settingsStore.baseUrl.first() }
        val original = chain.request()
        val configuredUrl = configured.toHttpUrlOrNull() ?: return chain.proceed(original)
        val rewritten = original.url.newBuilder()
            .scheme(configuredUrl.scheme)
            .host(configuredUrl.host)
            .port(configuredUrl.port)
            .build()
        return chain.proceed(original.newBuilder().url(rewritten).build())
    }
}
