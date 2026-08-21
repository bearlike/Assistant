package com.mewbo.aura.di

import android.content.Context
import android.media.AudioManager
import android.media.audiofx.LoudnessEnhancer
import android.speech.SpeechRecognizer
import com.mewbo.aura.data.api.SpeechApi
import com.mewbo.aura.data.model.SpeechDirection
import com.mewbo.aura.data.repo.SpeechRepository
import com.mewbo.aura.data.settings.SettingsStore
import com.mewbo.aura.voice.AudioBoostPlatform
import com.mewbo.aura.voice.BoostHandle
import com.mewbo.aura.voice.RemoteSynthesizer
import com.mewbo.aura.voice.RemoteTranscriber
import com.mewbo.aura.voice.SelectedSynthesizer
import com.mewbo.aura.voice.SelectedTranscriber
import com.mewbo.aura.voice.SpeechEngineGate
import com.mewbo.aura.voice.SpeechGateway
import com.mewbo.aura.voice.SpeechRecognitionAvailability
import com.mewbo.aura.voice.SpeechVolumeBoost
import com.mewbo.aura.voice.SpeechVolumeBoostGate
import com.mewbo.aura.voice.Synthesizer
import com.mewbo.aura.voice.Transcriber
import dagger.Binds
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import java.util.concurrent.TimeUnit
import javax.inject.Qualifier
import javax.inject.Singleton
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

/**
 * Qualifies the ON-DEVICE leg of each speech seam — the platform engine in release, the
 * fake/platform runtime switch in debug.
 *
 * It exists so the two build-type `VoiceModule`s keep owning the ONE thing they were always about
 * ("what does on-device mean in this variant?") while the routing above them stays variant-
 * agnostic and lives here, in `main`. Without the qualifier the routers and their own delegates
 * would both be `Transcriber`/`Synthesizer` and Dagger would bind a class to itself.
 */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class OnDeviceSpeech

/**
 * Qualifies the SERVER-BACKED leg of each speech seam.
 *
 * A qualifier rather than the concrete [com.mewbo.aura.voice.RemoteTranscriber]/
 * [com.mewbo.aura.voice.RemoteSynthesizer] types in the routers' constructors, so both legs are
 * plain interfaces and the routers stay constructible in a plain-JVM test. Naming the concrete
 * class would drag `Context`, `AudioManager` and `MediaPlayer` into every routing test to assert a
 * branch that touches none of them.
 */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class ServerSpeech

/** Qualifies the speech-only [Retrofit], which differs from the shared one in exactly one way —
 * a `callTimeout` above the server's own deadlines. See [SpeechModule.provideSpeechRetrofit]. */
@Retention(AnnotationRetention.BINARY)
@Qualifier
annotation class SpeechHttp

/**
 * Speech routing: the UNQUALIFIED [Transcriber]/[Synthesizer] every consumer injects resolve to the
 * user-selection routers, which fall back to the [OnDeviceSpeech] leg the build type supplies.
 *
 * The layering is the point. `src/debug` and `src/release` `VoiceModule` each answer only "what is
 * on-device here", this module answers "which engine does the user want", and no consumer answers
 * anything — so adding a fourth engine later is one more delegate in the routers, not an edit to
 * every call site.
 */
@Module
@InstallIn(SingletonComponent::class)
abstract class SpeechModule {

    @Binds
    abstract fun bindTranscriber(impl: SelectedTranscriber): Transcriber

    @Binds
    abstract fun bindSynthesizer(impl: SelectedSynthesizer): Synthesizer

    @Binds
    @ServerSpeech
    abstract fun bindServerTranscriber(impl: RemoteTranscriber): Transcriber

    @Binds
    @ServerSpeech
    abstract fun bindServerSynthesizer(impl: RemoteSynthesizer): Synthesizer

    /** The `voice/`-declared network seam, implemented in `data/repo` — the same
     * narrow-interface-DOWN + binding-HERE shape `DeviceToolDispatch` and `RunNotifications`
     * follow (this package's own CLAUDE.md). */
    @Binds
    abstract fun bindSpeechGateway(impl: SpeechRepository): SpeechGateway

    companion object {

        /**
         * A speech-only HTTP client, derived from the shared one so it inherits every
         * interceptor, the connection pool and the debug mock — the same `newBuilder()` trick
         * `DataModule.provideEventSourceFactory` uses for SSE, and for the same reason.
         *
         * **Only `callTimeout` differs, and it must EXCEED the server's own deadlines.** The
         * shared client caps a call at 30s; the speech routes are bounded server-side at 30s
         * (transcribe) and 60s (synthesize). At 30s the client TIES the first and UNDERCUTS the
         * second, so the client's own `SocketTimeoutException` wins the race and the caller gets
         * a generic transport failure in place of the server's diagnosable
         * `502 speech_gateway_timeout` — strictly less information about the same event, and it
         * defeats the distinct error codes the route publishes. Sitting above BOTH deadlines
         * means the server's answer always arrives first.
         *
         * One value rather than a per-route pair: a `callTimeout` cannot be varied per call from
         * an interceptor, so two values would mean two clients and two Retrofits for one bound
         * that is only ever a backstop. **The cost of the single higher value is bounded by
         * something else** — the inherited 10s `readTimeout` is untouched, so a dead socket still
         * fails in ~10s. This ceiling only ever applies to a server that is genuinely alive and
         * slow, which is exactly the case that should be allowed to finish.
         *
         * Sized against the SLOW measurement, never the warm one: a cold first synthesis measured
         * ~7.9s for a 34-character sentence (connection setup plus a server-side import) against
         * ~2.4s warm. The first press after a restart is the one a user notices, so a ceiling
         * tuned to the warm figure would cut off precisely the call most worth waiting for.
         */
        @Provides
        @Singleton
        @SpeechHttp
        fun provideSpeechRetrofit(okHttpClient: OkHttpClient, json: Json): Retrofit = Retrofit.Builder()
            // Rewritten per request by `BaseUrlInterceptor`, inherited with the client above —
            // this placeholder is never dialled, exactly as in `DataModule`.
            .baseUrl(PLACEHOLDER_BASE_URL)
            .client(
                okHttpClient.newBuilder()
                    .callTimeout(SPEECH_CALL_TIMEOUT_SECONDS, TimeUnit.SECONDS)
                    .build(),
            )
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()

        @Provides
        @Singleton
        fun provideSpeechApi(@SpeechHttp retrofit: Retrofit): SpeechApi = retrofit.create(SpeechApi::class.java)

        /** Above the server's longest speech deadline (60s, synthesize) with headroom, so the
         * server's diagnosable refusal always beats the client's generic one. */
        private const val SPEECH_CALL_TIMEOUT_SECONDS = 90L

        /** Never dialled — `BaseUrlInterceptor` rewrites it. Restated rather than shared because
         * `DataModule`'s copy is file-private. */
        private const val PLACEHOLDER_BASE_URL = "http://localhost/"

        /**
         * The selection seam, as a lambda over [SettingsStore] rather than a [SettingsStore]
         * injection into `voice/`.
         *
         * Same motivation as `DeviceToolGate`: `SettingsStore` reaches the Android Keystore through
         * `KeystoreCipher`, so injecting it would force every routing test onto Robolectric to
         * assert a branch that is pure. The routers take a flow of a bare string and stay
         * plain-JVM testable.
         */
        @Provides
        @Singleton
        fun provideSpeechEngineGate(settingsStore: SettingsStore): SpeechEngineGate =
            SpeechEngineGate { direction ->
                when (direction) {
                    SpeechDirection.SpeechToText -> settingsStore.speechToTextEngine
                    SpeechDirection.TextToSpeech -> settingsStore.textToSpeechEngine
                }
            }

        /** The boost level, as a lambda over [SettingsStore], for [provideSpeechEngineGate]'s
         * reason exactly — `SettingsStore` reaches the Android Keystore, and every rule worth
         * testing in [SpeechVolumeBoost] is pure. */
        @Provides
        @Singleton
        fun provideSpeechVolumeBoostGate(settingsStore: SettingsStore): SpeechVolumeBoostGate =
            SpeechVolumeBoostGate { settingsStore.speechVolumeBoostDecibels }

        /**
         * The real `android.media.audiofx` read behind [AudioBoostPlatform] — the only place in
         * the app that touches an audio effect, kept here beside
         * [provideSpeechRecognitionAvailability] for the same reason: it lets [SpeechVolumeBoost]'s
         * clamping, unit conversion and refusal latch be unit-tested with no `AudioManager`.
         *
         * **Every failure is swallowed into `null` deliberately.** `LoudnessEnhancer`'s constructor
         * declares four unchecked throwables and a device with no such effect library raises
         * `UnsupportedOperationException` — a boost that cannot be built must degrade to the
         * unmodified playback path, never take a spoken reply down with it. The `null` is not
         * silent: [SpeechVolumeBoost] latches it into [SpeechBoostState.Refused], which Settings
         * renders.
         *
         * **Never session 0.** Attaching to the global output mix is the one case AOSP gates on
         * `MODIFY_AUDIO_SETTINGS` (`AudioFlinger::createEffect`), and its own platform log calls it
         * deprecated — it would also amplify every other app's audio, which this control does not
         * promise. A generated per-session id is the whole mechanism.
         */
        @Provides
        @Singleton
        fun provideAudioBoostPlatform(@ApplicationContext context: Context): AudioBoostPlatform =
            object : AudioBoostPlatform {
                private val audioManager by lazy {
                    context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
                }

                override fun newSessionId(): Int = audioManager.generateAudioSessionId()

                override fun attachLoudness(sessionId: Int, gainMillibels: Int): BoostHandle? =
                    runCatching {
                        val effect = LoudnessEnhancer(sessionId)
                        effect.setTargetGain(gainMillibels)
                        effect.setEnabled(true)
                        BoostHandle { runCatching { effect.release() } }
                    }.getOrNull()
            }

        /**
         * The real platform read behind [SpeechRecognitionAvailability] — same predicate
         * `VoiceBackends` already evaluates for its own fake/platform switch, exposed here as a
         * narrow seam so [SelectedTranscriber] can fall back off an on-device selection that this
         * device cannot actually service, without dragging `Context`/`SpeechRecognizer` into its
         * unit tests.
         */
        @Provides
        @Singleton
        fun provideSpeechRecognitionAvailability(
            @ApplicationContext context: Context,
        ): SpeechRecognitionAvailability =
            SpeechRecognitionAvailability { SpeechRecognizer.isRecognitionAvailable(context) }
    }
}
