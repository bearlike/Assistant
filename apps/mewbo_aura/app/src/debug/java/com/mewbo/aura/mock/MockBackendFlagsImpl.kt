package com.mewbo.aura.mock

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.preferencesDataStore
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.runBlocking

private val Context.mockBackendDataStore: DataStore<Preferences> by preferencesDataStore(name = "mock_backend_settings")

/**
 * Debug-only, own small DataStore file - deliberately NOT [com.mewbo.aura.data.settings.SettingsStore]
 * (`data/` stays untouched, user directive). Default OFF: a fresh install/data-wipe never
 * accidentally serves scripted responses instead of hitting the real backend. Persisted (not
 * in-memory) so the toggle survives process death during a testing session, same rationale
 * `VoiceBackends`'s own DataStore had - but unlike that class, THIS store is the single source of
 * truth both [com.mewbo.aura.ui.settings.SettingsViewModel] (via this same injected interface) and
 * [MockBackendInterceptor] read, so there's no risk of the Settings-row-vs-actual-behavior
 * disconnect `VoiceBackends`' own toggle silently has.
 */
@Singleton
class MockBackendFlagsImpl @Inject constructor(
    @ApplicationContext private val context: Context,
) : MockBackendFlags {
    override val enabled: Flow<Boolean> =
        context.mockBackendDataStore.data.map { it[ENABLED_KEY] ?: false }

    override suspend fun setEnabled(value: Boolean) {
        context.mockBackendDataStore.edit { it[ENABLED_KEY] = value }
    }

    override fun isEnabledBlocking(): Boolean =
        runBlocking { enabled.first() }

    private companion object {
        val ENABLED_KEY = booleanPreferencesKey("mockBackend.enabled")
    }
}
