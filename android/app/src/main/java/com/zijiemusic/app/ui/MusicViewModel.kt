package com.zijiemusic.app.ui

import android.app.Application
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.zijiemusic.app.BuildConfig
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import com.zijiemusic.app.data.AppUpdateInfo
import com.zijiemusic.app.R
import com.zijiemusic.app.data.ConfigStore
import com.zijiemusic.app.data.NavidromeClient
import com.zijiemusic.app.data.ServerConfig
import com.zijiemusic.app.data.Song
import com.zijiemusic.app.playback.MusicPlayerHolder
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlin.random.Random

enum class PlayMode {
    Single,
    Sequential,
    Random
}

enum class QueueType {
    All,
    Favorites,
    Searches
}

data class MusicUiState(
    val config: ServerConfig = ServerConfig(),
    val songs: List<Song> = emptyList(),
    val currentSong: Song? = null,
    val loading: Boolean = false,
    val message: String = "",
    val connected: Boolean = false,
    val isPlaying: Boolean = false,
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
    val playMode: PlayMode = PlayMode.Sequential,
    val favoriteIds: Set<String> = emptySet(),
    val searchIds: Set<String> = emptySet(),
    val queueType: QueueType = QueueType.All,
    val updateInfo: AppUpdateInfo? = null,
    val updateMessage: String = "",
    val checkingUpdate: Boolean = false,
    val showQueue: Boolean = false,
    val showUpdateDialog: Boolean = false,
    val showSettings: Boolean = false,
    val licenseCode: String = "",
    val licenseInput: String = "",
    val licenseActive: Boolean = false,
    val licenseMessage: String = "",
    val trialDaysLeft: Int = 3,
    val trialExpired: Boolean = false,
    val showActivation: Boolean = false,
    val showSearch: Boolean = false,
    val searchQuery: String = "",
    val searchSuggestions: List<Song> = emptyList(),
    val searchLoading: Boolean = false,
    val searchMessage: String = ""
) {
    val activeSongs: List<Song>
        get() = when (queueType) {
            QueueType.Favorites -> songs.filter { it.id in favoriteIds }
            QueueType.Searches -> songs.filter { it.id in searchIds }
            QueueType.All -> songs
        }

    val currentIsFavorite: Boolean
        get() = currentSong?.id in favoriteIds
}

class MusicViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application
    private val store = ConfigStore(application)
    private val client = NavidromeClient()
    private val player = MusicPlayerHolder.get(application)
    private var progressJob: Job? = null
    private var suggestJob: Job? = null
    private var searchPollJob: Job? = null
    private var playerQueueIds: List<String> = emptyList()
    private val _state = MutableStateFlow(MusicUiState())
    val state: StateFlow<MusicUiState> = _state.asStateFlow()

    init {
        player.addListener(
            object : Player.Listener {
                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    _state.value = _state.value.copy(isPlaying = isPlaying)
                }

                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_ENDED) {
                        onTrackEnded()
                    }
                }

                override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
                    val songId = mediaItem?.mediaId ?: return
                    val song = _state.value.songs.firstOrNull { it.id == songId } ?: return
                    _state.value = _state.value.copy(currentSong = song)
                }

                override fun onPlayerError(error: PlaybackException) {
                    _state.value = _state.value.copy(
                        isPlaying = false,
                        message = "\u64ad\u653e\u4e2d\u65ad\uff0c\u8bf7\u91cd\u65b0\u70b9\u51fb\u64ad\u653e"
                    )
                }
            }
        )
        progressJob = viewModelScope.launch {
            while (true) {
                _state.value = _state.value.copy(
                    positionMs = player.currentPosition.coerceAtLeast(0L),
                    durationMs = player.duration.takeIf { it > 0L } ?: ((_state.value.currentSong?.durationSeconds ?: 0) * 1000L)
                )
                delay(500)
            }
        }
        viewModelScope.launch {
            store.config.collect { config ->
                _state.value = _state.value.copy(config = config)
                if (_state.value.songs.isEmpty()) {
                    loadSongs()
                }
            }
        }
        viewModelScope.launch {
            store.favoriteIds.collect { ids ->
                _state.value = _state.value.copy(favoriteIds = ids)
            }
        }
        viewModelScope.launch {
            store.searchIds.collect { ids ->
                _state.value = _state.value.copy(searchIds = ids)
            }
        }
        viewModelScope.launch {
            store.ensureInstallState(deviceId(), System.currentTimeMillis())
            store.license.collect { license ->
                val now = System.currentTimeMillis()
                val installTime = license.installTimeMs.takeIf { it > 0L } ?: now
                val elapsedDays = ((now - installTime) / (24L * 60L * 60L * 1000L)).toInt()
                val daysLeft = (3 - elapsedDays).coerceIn(0, 3)
                _state.value = _state.value.copy(
                    licenseCode = license.licenseCode,
                    licenseInput = _state.value.licenseInput.ifBlank { license.licenseCode },
                    trialDaysLeft = daysLeft,
                    trialExpired = daysLeft <= 0
                )
                checkLicenseStatus()
                loadServerSearchList()
            }
        }
        viewModelScope.launch {
            while (true) {
                sendHeartbeat()
                delay(60_000)
            }
        }
        viewModelScope.launch {
            delay(1200)
            checkUpdate(silent = true)
        }
    }

    fun updateConfig(config: ServerConfig) {
        _state.value = _state.value.copy(config = config)
    }

    fun saveAndConnect() {
        val config = _state.value.config.copy(baseUrl = _state.value.config.baseUrl.ifBlank { ServerConfig().baseUrl })
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = "")
            runCatching {
                store.save(config)
                client.getRandomSongs(config)
            }.onSuccess { songs ->
                _state.value = _state.value.copy(
                    config = config,
                    songs = songs,
                    currentSong = _state.value.currentSong ?: songs.firstOrNull(),
                    loading = false,
                    connected = true,
                    message = app.getString(R.string.connection_success),
                    showSettings = false
                )
                loadServerSearchList()
            }.onFailure {
                _state.value = _state.value.copy(
                    loading = false,
                    connected = false,
                    message = app.getString(R.string.connection_failed)
                )
            }
        }
    }

    fun loadSongs() {
        val config = _state.value.config.copy(baseUrl = _state.value.config.baseUrl.ifBlank { ServerConfig().baseUrl })
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = "")
            runCatching { client.getRandomSongs(config) }
                .onSuccess { songs ->
                    _state.value = _state.value.copy(
                        config = config,
                        songs = songs,
                        currentSong = _state.value.currentSong ?: songs.firstOrNull(),
                        loading = false,
                        connected = true,
                        message = if (songs.isEmpty()) app.getString(R.string.no_songs) else ""
                    )
                    loadServerSearchList()
                }
                .onFailure {
                    _state.value = _state.value.copy(
                        loading = false,
                        message = app.getString(R.string.refresh_failed)
                    )
                }
        }
    }

    fun play(song: Song) {
        if (!canUseApp()) return
        val queue = _state.value.activeSongs.ifEmpty { _state.value.songs }
        val songs = queue.ifEmpty { listOf(song) }
        val startIndex = songs.indexOfFirst { it.id == song.id }.takeIf { it >= 0 } ?: 0
        val queueIds = songs.map { it.id }
        if (playerQueueIds != queueIds || player.mediaItemCount != songs.size) {
            player.setMediaItems(songs.map { it.toMediaItem() }, startIndex, 0L)
            playerQueueIds = queueIds
        } else {
            player.seekToDefaultPosition(startIndex)
        }
        player.prepare()
        player.play()
        _state.value = _state.value.copy(currentSong = song, showQueue = false)
    }

    fun togglePlay() {
        if (!canUseApp()) return
        val current = _state.value.currentSong
        if (current == null) {
            _state.value.songs.firstOrNull()?.let { play(it) }
            return
        }
        if (player.mediaItemCount == 0) {
            play(current)
        } else if (player.isPlaying) {
            player.pause()
        } else if (player.playbackState == Player.STATE_IDLE || player.playbackState == Player.STATE_ENDED) {
            play(current)
        } else {
            player.prepare()
            player.play()
        }
    }

    fun previous() {
        if (!canUseApp()) return
        val previousSong = adjacentSong(-1) ?: return
        play(previousSong)
    }

    fun next() {
        if (!canUseApp()) return
        val nextSong = when (_state.value.playMode) {
            PlayMode.Random -> randomSong()
            else -> adjacentSong(1)
        } ?: return
        play(nextSong)
    }

    fun seekTo(progress: Float) {
        val duration = _state.value.durationMs
        if (duration > 0L) {
            player.seekTo((duration * progress.coerceIn(0f, 1f)).toLong())
        }
    }

    fun setPlayMode(mode: PlayMode) {
        _state.value = _state.value.copy(playMode = mode)
    }

    fun cyclePlayMode() {
        val next = when (_state.value.playMode) {
            PlayMode.Single -> PlayMode.Sequential
            PlayMode.Sequential -> PlayMode.Random
            PlayMode.Random -> PlayMode.Single
        }
        _state.value = _state.value.copy(playMode = next)
    }

    fun toggleFavorite(song: Song? = _state.value.currentSong) {
        val target = song ?: return
        val next = _state.value.favoriteIds.toMutableSet().apply {
            if (!add(target.id)) remove(target.id)
        }
        viewModelScope.launch {
            store.saveFavoriteIds(next)
        }
        val nextQueueType = if (_state.value.queueType == QueueType.Favorites && next.isEmpty()) QueueType.All else _state.value.queueType
        _state.value = _state.value.copy(favoriteIds = next, queueType = nextQueueType)
    }

    fun setQueueType(type: QueueType) {
        _state.value = _state.value.copy(queueType = type)
    }

    fun showSearch(show: Boolean) {
        _state.value = _state.value.copy(showSearch = show)
        if (!show) {
            suggestJob?.cancel()
        }
    }

    fun updateSearchQuery(value: String) {
        _state.value = _state.value.copy(searchQuery = value, searchMessage = "")
        suggestJob?.cancel()
        val query = value.trim()
        if (query.isBlank()) {
            _state.value = _state.value.copy(searchSuggestions = emptyList())
            return
        }
        suggestJob = viewModelScope.launch {
            delay(350)
            runCatching { client.searchSuggestions(_state.value.config, query) }
                .onSuccess { suggestions ->
                    if (_state.value.searchQuery.trim() == query) {
                        _state.value = _state.value.copy(searchSuggestions = suggestions)
                    }
                }
        }
    }

    fun submitSearch(query: String = _state.value.searchQuery) {
        if (!canUseApp()) return
        val keyword = query.trim()
        if (keyword.length < 2) {
            _state.value = _state.value.copy(searchMessage = "\u8bf7\u8f93\u5165\u81f3\u5c11 2 \u4e2a\u5b57")
            return
        }
        searchPollJob?.cancel()
        viewModelScope.launch {
            val location = lastLocation()
            _state.value = _state.value.copy(searchLoading = true, searchMessage = "\u6b63\u5728\u63d0\u4ea4\u641c\u7d22")
            runCatching {
                client.startSearchDownload(
                    _state.value.config,
                    keyword,
                    _state.value.licenseCode,
                    deviceId(),
                    BuildConfig.VERSION_NAME,
                    deviceModel(),
                    location?.latitude,
                    location?.longitude
                )
            }.onSuccess { response ->
                handleSearchResponse(response.jobId, response.status, response.percent, response.message, response.track)
            }.onFailure {
                _state.value = _state.value.copy(searchLoading = false, searchMessage = it.message ?: "\u641c\u7d22\u5931\u8d25")
            }
        }
    }

    fun checkUpdate(silent: Boolean = false) {
        val config = _state.value.config.copy(baseUrl = _state.value.config.baseUrl.ifBlank { ServerConfig().baseUrl })
        viewModelScope.launch {
            _state.value = _state.value.copy(checkingUpdate = true, updateMessage = "")
            runCatching { client.latestApp(config) }
                .onSuccess { info ->
                    val hasNewVersion = info.hasUpdate && info.versionCode > BuildConfig.VERSION_CODE
                    _state.value = _state.value.copy(
                        checkingUpdate = false,
                        updateInfo = info.takeIf { hasNewVersion },
                        showUpdateDialog = hasNewVersion,
                        updateMessage = when {
                            hasNewVersion -> "\u53d1\u73b0\u65b0\u7248\u672c ${info.versionName}"
                            silent -> ""
                            else -> "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c"
                        }
                    )
                    loadServerSearchList()
                }
                .onFailure {
                    _state.value = _state.value.copy(
                        checkingUpdate = false,
                        updateMessage = if (silent) "" else "\u68c0\u67e5\u66f4\u65b0\u5931\u8d25"
                    )
                }
        }
    }

    fun dismissUpdate() {
        _state.value = _state.value.copy(showUpdateDialog = false)
    }

    fun showQueue(show: Boolean) {
        _state.value = _state.value.copy(showQueue = show)
    }

    fun showSettings(show: Boolean) {
        _state.value = _state.value.copy(showSettings = show)
    }

    fun downloadUrl(song: Song): String = client.downloadUrl(_state.value.config, song.id)

    fun updateLicenseInput(value: String) {
        _state.value = _state.value.copy(licenseInput = value.take(6).uppercase())
    }

    fun showActivation(show: Boolean) {
        _state.value = _state.value.copy(showActivation = show)
    }

    fun activateLicense() {
        val code = _state.value.licenseInput.trim().uppercase()
        if (code.length != 6) {
            _state.value = _state.value.copy(licenseMessage = "\u8bf7\u8f93\u5165 6 \u4f4d\u5e8f\u5217\u53f7")
            return
        }
        viewModelScope.launch {
            runCatching {
                val location = lastLocation()
                client.activateLicense(
                    _state.value.config,
                    code,
                    deviceId(),
                    BuildConfig.VERSION_NAME,
                    deviceModel(),
                    location?.latitude,
                    location?.longitude
                )
            }.onSuccess { response ->
                if (response.active) {
                    store.saveLicenseCode(code)
                }
                _state.value = _state.value.copy(
                    licenseActive = response.active,
                    licenseCode = code.takeIf { response.active } ?: _state.value.licenseCode,
                    trialExpired = false.takeIf { response.active } ?: _state.value.trialExpired,
                    licenseMessage = response.message.ifBlank { if (response.active) "\u5df2\u6fc0\u6d3b" else "\u6fc0\u6d3b\u5931\u8d25" }
                )
            }.onFailure {
                _state.value = _state.value.copy(licenseActive = false, licenseMessage = it.message ?: "\u6fc0\u6d3b\u5931\u8d25")
            }
        }
    }

    private fun onTrackEnded() {
        when (_state.value.playMode) {
            PlayMode.Single -> {
                player.seekTo(0L)
                player.play()
            }
            PlayMode.Sequential -> next()
            PlayMode.Random -> randomSong()?.let { play(it) }
        }
    }

    private fun canUseApp(): Boolean {
        val allowed = _state.value.licenseActive || !_state.value.trialExpired
        if (!allowed) {
            _state.value = _state.value.copy(licenseMessage = "\u8bd5\u7528\u5df2\u5230\u671f\uff0c\u8bf7\u52a0\u5fae\u4fe1 ABCD59113 \u83b7\u53d6\u5e8f\u5217\u53f7")
        }
        return allowed
    }

    private fun checkLicenseStatus() {
        val code = _state.value.licenseCode
        if (code.isBlank()) return
        viewModelScope.launch {
            runCatching {
                val location = lastLocation()
                client.licenseStatus(_state.value.config, code, deviceId(), BuildConfig.VERSION_NAME, deviceModel(), location?.latitude, location?.longitude)
            }.onSuccess { response ->
                _state.value = _state.value.copy(licenseActive = response.active, licenseMessage = response.message)
            }
        }
    }

    private suspend fun sendHeartbeat() {
        val code = _state.value.licenseCode
        if (code.isBlank()) return
        runCatching {
            val location = lastLocation()
            client.licenseHeartbeat(_state.value.config, code, deviceId(), BuildConfig.VERSION_NAME, deviceModel(), location?.latitude, location?.longitude)
        }
    }

    private fun loadServerSearchList() {
        viewModelScope.launch {
            runCatching { client.searchList(_state.value.config, _state.value.licenseCode, deviceId()) }
                .onSuccess { songs ->
                    if (songs.isEmpty()) return@onSuccess
                    val mergedSongs = (_state.value.songs + songs).distinctBy { it.id }
                    val mergedIds = _state.value.searchIds + songs.map { it.id }
                    store.saveSearchIds(mergedIds)
                    _state.value = _state.value.copy(songs = mergedSongs, searchIds = mergedIds)
                }
        }
    }

    private fun handleSearchResponse(jobId: String, status: String, percent: Int, message: String, track: Song?, schedulePoll: Boolean = true) {
        val label = message.ifBlank {
            when (status) {
                "queued" -> "\u5df2\u52a0\u5165\u4e0b\u8f7d\u961f\u5217"
                "downloading" -> "\u6b63\u5728\u4e0b\u8f7d"
                "importing" -> "\u6b63\u5728\u5bfc\u5165"
                "success" -> "\u641c\u7d22\u4e0b\u8f7d\u5b8c\u6210"
                "failed" -> "\u641c\u7d22\u4e0b\u8f7d\u5931\u8d25"
                else -> "\u6b63\u5728\u5904\u7406"
            }
        }
        val finished = status in setOf("success", "failed")
        _state.value = _state.value.copy(
            searchLoading = !finished,
            searchMessage = "$label ${percent.coerceIn(0, 100)}%"
        )
        if (track != null && status == "success") {
            addSearchTrack(track)
            loadSongs()
            return
        }
        if (schedulePoll && jobId.isNotBlank() && !finished) {
            pollSearchJob(jobId)
        }
    }

    private fun pollSearchJob(jobId: String) {
        searchPollJob?.cancel()
        searchPollJob = viewModelScope.launch {
            repeat(120) {
                delay(1500)
                runCatching { client.searchStatus(_state.value.config, jobId) }
                    .onSuccess { response ->
                        handleSearchResponse(response.jobId, response.status, response.percent, response.message, response.track, schedulePoll = false)
                        if (response.status in setOf("success", "failed")) {
                            return@launch
                        }
                    }
                    .onFailure {
                        _state.value = _state.value.copy(searchLoading = false, searchMessage = it.message ?: "\u67e5\u8be2\u4efb\u52a1\u5931\u8d25")
                        return@launch
                    }
            }
            _state.value = _state.value.copy(searchLoading = false, searchMessage = "\u4e0b\u8f7d\u4efb\u52a1\u8d85\u65f6")
        }
    }

    private fun addSearchTrack(song: Song) {
        val mergedSongs = (_state.value.songs + song).distinctBy { it.id }
        val mergedIds = _state.value.searchIds + song.id
        viewModelScope.launch {
            store.saveSearchIds(mergedIds)
        }
        _state.value = _state.value.copy(
            songs = mergedSongs,
            searchIds = mergedIds,
            queueType = QueueType.Searches,
            searchSuggestions = emptyList(),
            searchLoading = false,
            searchMessage = "\u5df2\u52a0\u5165\u6211\u7684\u641c\u7d22"
        )
    }

    private fun deviceId(): String {
        return Settings.Secure.getString(app.contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
    }

    private fun deviceModel(): String {
        return "${Build.MANUFACTURER} ${Build.MODEL}".trim()
    }

    private fun lastLocation(): Location? {
        if (
            ContextCompat.checkSelfPermission(app, android.Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(app, android.Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED
        ) {
            return null
        }
        val manager = app.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        return listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .mapNotNull { provider -> runCatching { manager.getLastKnownLocation(provider) }.getOrNull() }
            .maxByOrNull { it.time }
    }

    private fun adjacentSong(offset: Int): Song? {
        val songs = _state.value.activeSongs
        if (songs.isEmpty()) return null
        val current = _state.value.currentSong
        val currentIndex = songs.indexOfFirst { it.id == current?.id }.takeIf { it >= 0 } ?: 0
        return songs[(currentIndex + offset + songs.size) % songs.size]
    }

    private fun randomSong(): Song? {
        val songs = _state.value.activeSongs
        if (songs.isEmpty()) return null
        if (songs.size == 1) return songs.first()
        val currentId = _state.value.currentSong?.id
        return songs.filterNot { it.id == currentId }.random(Random(System.currentTimeMillis()))
    }

    private fun Song.toMediaItem(): MediaItem {
        val url = streamUrl.ifBlank { client.streamUrl(_state.value.config, id) }
        return MediaItem.Builder()
            .setMediaId(id)
            .setUri(Uri.parse(url))
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title.ifBlank { app.getString(R.string.unknown_song) })
                    .setArtist(artist.ifBlank { app.getString(R.string.unknown_artist) })
                    .setAlbumTitle(album.ifBlank { app.getString(R.string.unknown_album) })
                    .build()
            )
            .build()
    }

    override fun onCleared() {
        progressJob?.cancel()
        suggestJob?.cancel()
        searchPollJob?.cancel()
        super.onCleared()
    }
}
