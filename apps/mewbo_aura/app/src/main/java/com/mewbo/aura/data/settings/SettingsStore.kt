package com.mewbo.aura.data.settings

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext

/** Defined at file scope (not inside the class) so exactly one DataStore instance ever exists per file. */
private val Context.auraDataStore: DataStore<Preferences> by preferencesDataStore(name = "aura_settings")

/**
 * DataStore-preferences-backed settings. The API key is encrypted at rest via [KeystoreCipher] -
 * callers only ever see/set plaintext; ciphertext + IV never leave this class.
 */
class SettingsStore @Inject constructor(
    @ApplicationContext private val context: Context,
    private val keystoreCipher: KeystoreCipher,
) {
    val baseUrl: Flow<String> = context.auraDataStore.data.map { it[KEY_BASE_URL] ?: DEFAULT_BASE_URL }

    suspend fun setBaseUrl(value: String) {
        context.auraDataStore.edit { it[KEY_BASE_URL] = value }
    }

    // KeystoreCipher.decrypt is a synchronous Android Keystore/TEE
    // IPC call (same trap class ConnectionProbe's own OkHttp/body.string() fix guards against) -
    // .map{} runs on the collector's own dispatcher (often Main, e.g. a Settings-screen
    // collectAsStateWithLifecycle), so this must be pinned off it explicitly. flowOn applies to the
    // whole upstream map{} lambda, keeping the seam a single Flow operator, still testable the same way.
    val apiKey: Flow<String?> = context.auraDataStore.data.map { prefs ->
        val ciphertext = prefs[KEY_API_KEY_CIPHERTEXT] ?: return@map null
        val iv = prefs[KEY_API_KEY_IV] ?: return@map null
        runCatching { keystoreCipher.decrypt(KeystoreCipher.EncryptedPayload(ciphertext, iv)) }.getOrNull()
    }.flowOn(Dispatchers.IO)

    suspend fun setApiKey(plain: String) {
        val payload = withContext(Dispatchers.IO) { keystoreCipher.encrypt(plain) }
        context.auraDataStore.edit {
            it[KEY_API_KEY_CIPHERTEXT] = payload.ciphertext
            it[KEY_API_KEY_IV] = payload.iv
        }
    }

    val speakResponses: Flow<Boolean> = context.auraDataStore.data.map { it[KEY_SPEAK_RESPONSES] ?: true }

    suspend fun setSpeakResponses(value: Boolean) {
        context.auraDataStore.edit { it[KEY_SPEAK_RESPONSES] = value }
    }

    val reducedMotion: Flow<Boolean> = context.auraDataStore.data.map { it[KEY_REDUCED_MOTION] ?: false }

    suspend fun setReducedMotion(value: Boolean) {
        context.auraDataStore.edit { it[KEY_REDUCED_MOTION] = value }
    }

    /** Passthrough toggle consumed by voice/ to select Fake vs. platform Transcriber/Synthesizer. */
    val voiceUseFakes: Flow<Boolean> = context.auraDataStore.data.map { it[KEY_VOICE_USE_FAKES] ?: false }

    suspend fun setVoiceUseFakes(value: Boolean) {
        context.auraDataStore.edit { it[KEY_VOICE_USE_FAKES] = value }
    }

    /**
     * Local "Your name" field (spec §6.14): the backend has no user profile, so the greeting's
     * first name and the drawer footer's display name both come from here. Empty by default -
     * callers hide name-dependent UI (greeting falls back to "Let's get into it", drawer footer
     * hides the name/avatar row) rather than showing a placeholder name.
     */
    val displayName: Flow<String> = context.auraDataStore.data.map { it[KEY_DISPLAY_NAME] ?: "" }

    suspend fun setDisplayName(value: String) {
        context.auraDataStore.edit { it[KEY_DISPLAY_NAME] = value }
    }

    /** The FULL-APP default model (bare, unprefixed - a raw `GET api/models` entry), also written by
     * the chat top-bar picker so a pick in the app becomes the new-chat default (`ChatViewModel
     * .globalModelPreference`). The assist overlay has its OWN default, [overlayDefaultModel]
     * - the two are independently persisted so a voice trigger and the app can run
     * different models. Empty = use the API's configured default; not a secret, so plain (unlike
     * [apiKey]). */
    val selectedModel: Flow<String> = context.auraDataStore.data.map { it[KEY_SELECTED_MODEL] ?: "" }

    suspend fun setSelectedModel(value: String) {
        context.auraDataStore.edit { it[KEY_SELECTED_MODEL] = value }
    }

    /** The assist-overlay default model, independent of [selectedModel]: read at
     * the overlay's OWN session-creation seam (`AuraSession`'s `createSession`/`sendQuery`) so a
     * voice-triggered turn can run a different model than the app. Empty = the API's configured
     * default, i.e. the pre-F3 behavior. There is no in-overlay picker, so this is edited only from
     * Settings ("Default model — assistant overlay"). */
    val overlayDefaultModel: Flow<String> = context.auraDataStore.data.map { it[KEY_OVERLAY_DEFAULT_MODEL] ?: "" }

    suspend fun setOverlayDefaultModel(value: String) {
        context.auraDataStore.edit { it[KEY_OVERLAY_DEFAULT_MODEL] = value }
    }

    /**
     * Tool ids the user has switched OFF in Settings' "Device tools" section.
     * Empty by default = every `device_*` tool enabled, preserving the pre-toggle behavior. Read
     * through the [com.mewbo.aura.data.device.DeviceToolGate] seam at the ONE catalog gate
     * (`DeviceToolCatalog.availableTools` intersects it with runtime-permission availability, so a
     * disabled tool is never advertised) AND at execution (`DeviceToolExecutor` refuses a disabled
     * tool with a `tool_disabled` error, since a stale server could still dispatch one).
     */
    val disabledDeviceToolIds: Flow<Set<String>> =
        context.auraDataStore.data.map { it[KEY_DISABLED_DEVICE_TOOL_IDS] ?: emptySet() }

    suspend fun setDeviceToolEnabled(toolId: String, enabled: Boolean) {
        context.auraDataStore.edit { prefs ->
            val current = prefs[KEY_DISABLED_DEVICE_TOOL_IDS] ?: emptySet()
            prefs[KEY_DISABLED_DEVICE_TOOL_IDS] = if (enabled) current - toolId else current + toolId
        }
    }

    /**
     * "Streamlit widgets" toggle. Default ON now that the renderer exists:
     * the flag gates BOTH the `stlite` capability advertisement
     * ([com.mewbo.aura.di.AuthInterceptor]) AND the `widget_ready` rendering
     * ([com.mewbo.aura.ui.chat.widget.WidgetCard]) at the SAME seam, so the app never advertises a
     * capability it won't service. Turning it OFF reverts to a plain chat client (no `stlite` header,
     * so the backend's `widget_builder` never fires) - a user escape hatch, not the default.
     */
    val streamlitWidgetsEnabled: Flow<Boolean> = context.auraDataStore.data.map { it[KEY_STREAMLIT_WIDGETS] ?: true }

    suspend fun setStreamlitWidgetsEnabled(value: Boolean) {
        context.auraDataStore.edit { it[KEY_STREAMLIT_WIDGETS] = value }
    }

    /** Default project context key (`ProjectSummary.contextKey`: bare name or `managed:<id>`).
     * Empty = Temporary directory. Seeds a fresh chat's scope in BOTH hosts (docked + overlay);
     * the composer options sheet can still override it per-chat before the session exists. */
    val selectedProject: Flow<String> = context.auraDataStore.data.map { it[KEY_SELECTED_PROJECT] ?: "" }

    suspend fun setSelectedProject(value: String) {
        context.auraDataStore.edit { it[KEY_SELECTED_PROJECT] = value }
    }

    private companion object {
        val KEY_BASE_URL = stringPreferencesKey("base_url")
        val KEY_API_KEY_CIPHERTEXT = stringPreferencesKey("api_key_ciphertext")
        val KEY_API_KEY_IV = stringPreferencesKey("api_key_iv")
        val KEY_SPEAK_RESPONSES = booleanPreferencesKey("speak_responses")
        val KEY_REDUCED_MOTION = booleanPreferencesKey("reduced_motion")
        val KEY_VOICE_USE_FAKES = booleanPreferencesKey("voice_use_fakes")
        val KEY_DISPLAY_NAME = stringPreferencesKey("display_name")
        val KEY_SELECTED_MODEL = stringPreferencesKey("selected_model")
        val KEY_OVERLAY_DEFAULT_MODEL = stringPreferencesKey("overlay_default_model")
        val KEY_SELECTED_PROJECT = stringPreferencesKey("selected_project")
        val KEY_DISABLED_DEVICE_TOOL_IDS = stringSetPreferencesKey("disabled_device_tool_ids")
        val KEY_STREAMLIT_WIDGETS = booleanPreferencesKey("streamlit_widgets_enabled")

        /** Empty by design - forces onboarding through Settings rather than guessing a host. */
        const val DEFAULT_BASE_URL = ""
    }
}
