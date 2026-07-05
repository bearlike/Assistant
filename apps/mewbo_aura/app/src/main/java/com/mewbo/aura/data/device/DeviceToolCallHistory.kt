package com.mewbo.aura.data.device

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/** Defined at file scope (not inside the class) so exactly one DataStore instance ever exists per
 * file - same pattern as [com.mewbo.aura.data.settings.SettingsStore]'s own store. A separate
 * store file (rather than piggybacking on [com.mewbo.aura.data.settings.SettingsStore]'s) so this
 * bounded call-id list doesn't churn on every settings write and vice versa. */
private val Context.deviceToolCallHistoryDataStore: DataStore<Preferences> by preferencesDataStore(name = "device_tool_call_history")

/** DataStore-preferences-backed [DeviceToolCallLedger] - the same persistence mechanism
 * [com.mewbo.aura.data.settings.SettingsStore] uses for the base URL/API key (task brief: "use
 * the app's existing persistence mechanism"). Keeps only the last [MAX_HISTORY] call ids, oldest
 * dropped first. */
class DeviceToolCallHistory @Inject constructor(
    @ApplicationContext private val context: Context,
    private val json: Json,
) : DeviceToolCallLedger {

    override suspend fun recordIfNew(callId: String): Boolean {
        var isNew = false
        context.deviceToolCallHistoryDataStore.edit { prefs ->
            val seen = decode(prefs[KEY_SEEN])
            if (callId in seen) {
                isNew = false
            } else {
                isNew = true
                prefs[KEY_SEEN] = json.encodeToString((seen + callId).takeLast(MAX_HISTORY))
            }
        }
        return isNew
    }

    private fun decode(raw: String?): List<String> {
        if (raw == null) return emptyList()
        return try {
            json.decodeFromString(raw)
        } catch (e: SerializationException) {
            emptyList()
        }
    }

    private companion object {
        val KEY_SEEN = stringPreferencesKey("seen_call_ids")
        const val MAX_HISTORY = 256
    }
}
