package com.zijiemusic.app.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "server_config")

class ConfigStore(private val context: Context) {
    private val baseUrlKey = stringPreferencesKey("base_url")
    private val usernameKey = stringPreferencesKey("username")
    private val passwordKey = stringPreferencesKey("password")
    private val favoriteIdsKey = stringPreferencesKey("favorite_ids")
    private val searchIdsKey = stringPreferencesKey("search_ids")
    private val installTimeKey = longPreferencesKey("install_time")
    private val licenseCodeKey = stringPreferencesKey("license_code")
    private val deviceIdKey = stringPreferencesKey("device_id")

    val config: Flow<ServerConfig> = context.dataStore.data.map { preferences ->
        ServerConfig(
            baseUrl = preferences[baseUrlKey] ?: ServerConfig().baseUrl,
            username = preferences[usernameKey] ?: "",
            password = preferences[passwordKey] ?: ""
        )
    }

    suspend fun save(config: ServerConfig) {
        context.dataStore.edit { preferences ->
            preferences[baseUrlKey] = config.baseUrl.trim().trimEnd('/')
            preferences[usernameKey] = config.username.trim()
            preferences[passwordKey] = config.password
        }
    }

    val favoriteIds: Flow<Set<String>> = context.dataStore.data.map { preferences ->
        preferences[favoriteIdsKey]
            ?.split(',')
            ?.map { it.trim() }
            ?.filter { it.isNotBlank() }
            ?.toSet()
            ?: emptySet()
    }

    suspend fun saveFavoriteIds(ids: Set<String>) {
        context.dataStore.edit { preferences ->
            preferences[favoriteIdsKey] = ids.joinToString(",")
        }
    }

    val searchIds: Flow<Set<String>> = context.dataStore.data.map { preferences ->
        preferences[searchIdsKey]
            ?.split(',')
            ?.map { it.trim() }
            ?.filter { it.isNotBlank() }
            ?.toSet()
            ?: emptySet()
    }

    suspend fun saveSearchIds(ids: Set<String>) {
        context.dataStore.edit { preferences ->
            preferences[searchIdsKey] = ids.joinToString(",")
        }
    }

    val license: Flow<LicenseState> = context.dataStore.data.map { preferences ->
        LicenseState(
            installTimeMs = preferences[installTimeKey] ?: 0L,
            licenseCode = preferences[licenseCodeKey] ?: "",
            deviceId = preferences[deviceIdKey] ?: ""
        )
    }

    suspend fun ensureInstallState(deviceId: String, nowMs: Long) {
        context.dataStore.edit { preferences ->
            if ((preferences[installTimeKey] ?: 0L) <= 0L) {
                preferences[installTimeKey] = nowMs
            }
            if ((preferences[deviceIdKey] ?: "").isBlank()) {
                preferences[deviceIdKey] = deviceId
            }
        }
    }

    suspend fun saveLicenseCode(code: String) {
        context.dataStore.edit { preferences ->
            preferences[licenseCodeKey] = code.trim().uppercase()
        }
    }
}

data class LicenseState(
    val installTimeMs: Long = 0L,
    val licenseCode: String = "",
    val deviceId: String = ""
)
