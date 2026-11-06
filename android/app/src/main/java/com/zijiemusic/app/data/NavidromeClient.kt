package com.zijiemusic.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

data class AppUpdateInfo(
    val hasUpdate: Boolean,
    val versionName: String = "",
    val versionCode: Int = 0,
    val releaseNotes: String = "",
    val size: Long = 0L,
    val downloadUrl: String = ""
)

data class LicenseResponse(
    val active: Boolean,
    val code: String = "",
    val message: String = ""
)

data class SearchJobResponse(
    val jobId: String = "",
    val status: String = "",
    val percent: Int = 0,
    val message: String = "",
    val track: Song? = null
)

class NavidromeClient {
    private val client = OkHttpClient()

    suspend fun getRandomSongs(config: ServerConfig, size: Int = 1000): List<Song> = withContext(Dispatchers.IO) {
        val url = "${config.normalizedBaseUrl()}/api/mobile/tracks".toHttpUrl().newBuilder().build()
        val body = execute(url.toString())
        val songs = JSONArray(body)
        buildList {
            for (index in 0 until songs.length()) {
                val item = songs.getJSONObject(index)
                add(songFromJson(config, item))
            }
        }.take(size)
    }

    suspend fun searchSuggestions(config: ServerConfig, query: String): List<Song> = withContext(Dispatchers.IO) {
        if (query.trim().isBlank()) return@withContext emptyList()
        val url = "${config.normalizedBaseUrl()}/api/mobile/search/suggest"
            .toHttpUrl()
            .newBuilder()
            .addQueryParameter("q", query.trim())
            .addQueryParameter("limit", "20")
            .build()
        val items = JSONArray(execute(url.toString()))
        buildList {
            for (index in 0 until items.length()) {
                add(songFromJson(config, items.getJSONObject(index)))
            }
        }
    }

    suspend fun searchList(config: ServerConfig, code: String, deviceId: String): List<Song> = withContext(Dispatchers.IO) {
        val url = "${config.normalizedBaseUrl()}/api/mobile/search/list"
            .toHttpUrl()
            .newBuilder()
            .addQueryParameter("code", code.trim().uppercase())
            .addQueryParameter("deviceId", deviceId)
            .build()
        val items = JSONArray(execute(url.toString()))
        buildList {
            for (index in 0 until items.length()) {
                val track = items.getJSONObject(index).optJSONObject("track")
                if (track != null) {
                    add(songFromJson(config, track))
                }
            }
        }
    }

    fun streamUrl(config: ServerConfig, songId: String): String {
        return "${config.normalizedBaseUrl()}/api/mobile/tracks/$songId/stream"
    }

    fun downloadUrl(config: ServerConfig, songId: String): String {
        return "${config.normalizedBaseUrl()}/api/mobile/tracks/$songId/download"
    }

    suspend fun latestApp(config: ServerConfig): AppUpdateInfo = withContext(Dispatchers.IO) {
        val url = "${config.normalizedBaseUrl()}/api/app/latest".toHttpUrl().newBuilder().build()
        val root = JSONObject(execute(url.toString()))
        AppUpdateInfo(
            hasUpdate = root.optBoolean("hasUpdate", false),
            versionName = root.optString("versionName"),
            versionCode = root.optInt("versionCode", 0),
            releaseNotes = root.optString("releaseNotes"),
            size = root.optLong("size", 0L),
            downloadUrl = root.optString("downloadUrl")
        )
    }

    suspend fun activateLicense(
        config: ServerConfig,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): LicenseResponse = withContext(Dispatchers.IO) {
        licensePost(config, "/api/mobile/license/activate", code, deviceId, appVersion, deviceModel, lat, lng)
    }

    suspend fun licenseStatus(
        config: ServerConfig,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): LicenseResponse = withContext(Dispatchers.IO) {
        licensePost(config, "/api/mobile/license/status", code, deviceId, appVersion, deviceModel, lat, lng)
    }

    suspend fun licenseHeartbeat(
        config: ServerConfig,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): LicenseResponse = withContext(Dispatchers.IO) {
        licensePost(config, "/api/mobile/license/heartbeat", code, deviceId, appVersion, deviceModel, lat, lng)
    }

    suspend fun startSearchDownload(
        config: ServerConfig,
        query: String,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): SearchJobResponse = withContext(Dispatchers.IO) {
        searchPost(config, "/api/mobile/search/download", query, code, deviceId, appVersion, deviceModel, lat, lng)
    }

    suspend fun searchStatus(config: ServerConfig, jobId: String): SearchJobResponse = withContext(Dispatchers.IO) {
        val root = JSONObject(execute("${config.normalizedBaseUrl()}/api/mobile/search/status/$jobId"))
        searchJobFromJson(config, root)
    }

    private fun ServerConfig.normalizedBaseUrl(): String {
        return baseUrl.ifBlank { ServerConfig().baseUrl }.trim().trimEnd('/')
    }

    private fun absoluteUrl(config: ServerConfig, path: String): String {
        if (path.startsWith("http://") || path.startsWith("https://")) {
            return path
        }
        return "${config.normalizedBaseUrl()}/${path.trimStart('/')}"
    }

    private fun songFromJson(config: ServerConfig, item: JSONObject): Song {
        return Song(
            id = item.getString("id"),
            title = item.optString("title"),
            artist = item.optString("artist"),
            album = item.optString("album"),
            durationSeconds = item.optInt("durationSeconds", 0),
            streamUrl = absoluteUrl(config, item.optString("streamUrl")),
            downloadUrl = absoluteUrl(config, item.optString("downloadUrl"))
        )
    }

    private fun searchJobFromJson(config: ServerConfig, root: JSONObject): SearchJobResponse {
        val trackJson = root.optJSONObject("track")
        return SearchJobResponse(
            jobId = root.optString("jobId"),
            status = root.optString("status"),
            percent = root.optInt("percent", 0),
            message = root.optString("message"),
            track = trackJson?.let { songFromJson(config, it) }
        )
    }

    private fun execute(url: String): String {
        val request = Request.Builder().url(url).build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                error("HTTP ${response.code}")
            }
            return response.body?.string() ?: ""
        }
    }

    private fun licensePost(
        config: ServerConfig,
        path: String,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): LicenseResponse {
        val root = JSONObject()
            .put("code", code.trim().uppercase())
            .put("deviceId", deviceId)
            .put("appVersion", appVersion)
            .put("deviceModel", deviceModel)
        if (lat != null && lng != null) {
            root.put("lat", lat)
            root.put("lng", lng)
        }
        val body = root.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("${config.normalizedBaseUrl()}$path")
            .post(body)
            .build()
        client.newCall(request).execute().use { response ->
            val responseText = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                val message = runCatching { JSONObject(responseText).optString("detail") }.getOrDefault("")
                error(message.ifBlank { "HTTP ${response.code}" })
            }
            val data = JSONObject(responseText)
            return LicenseResponse(
                active = data.optBoolean("active", false),
                code = data.optString("code"),
                message = data.optString("message")
            )
        }
    }

    private fun searchPost(
        config: ServerConfig,
        path: String,
        query: String,
        code: String,
        deviceId: String,
        appVersion: String,
        deviceModel: String,
        lat: Double?,
        lng: Double?
    ): SearchJobResponse {
        val root = JSONObject()
            .put("query", query.trim())
            .put("code", code.trim().uppercase())
            .put("deviceId", deviceId)
            .put("appVersion", appVersion)
            .put("deviceModel", deviceModel)
        if (lat != null && lng != null) {
            root.put("lat", lat)
            root.put("lng", lng)
        }
        val body = root.toString().toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("${config.normalizedBaseUrl()}$path")
            .post(body)
            .build()
        client.newCall(request).execute().use { response ->
            val responseText = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                val message = runCatching { JSONObject(responseText).optString("detail") }.getOrDefault("")
                error(message.ifBlank { "HTTP ${response.code}" })
            }
            return searchJobFromJson(config, JSONObject(responseText))
        }
    }
}
