package com.zijiemusic.app.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.FavoriteBorder
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.zijiemusic.app.R
import com.zijiemusic.app.data.AppUpdateInfo
import com.zijiemusic.app.data.ServerConfig
import com.zijiemusic.app.data.Song

@Composable
fun MusicApp(viewModel: MusicViewModel) {
    val state by viewModel.state.collectAsState()
    val current = state.currentSong
    val updateInfo = state.updateInfo
    val context = LocalContext.current

    MaterialTheme {
        Surface(modifier = Modifier.fillMaxSize(), color = Color(0xFFF7FAFF)) {
            Box(Modifier.fillMaxSize()) {
                PlayerScreen(
                    state = state,
                    onTogglePlay = viewModel::togglePlay,
                    onPrevious = viewModel::previous,
                    onNext = viewModel::next,
                    onSeek = viewModel::seekTo,
                    onCycleMode = viewModel::cyclePlayMode,
                    onQueue = { viewModel.showQueue(true) },
                    onSettings = { viewModel.showSettings(true) },
                    onActivation = { viewModel.showActivation(true) },
                    onRefresh = viewModel::loadSongs,
                    onFavorite = { viewModel.toggleFavorite() },
                    onSearch = { viewModel.showSearch(true) },
                    onShare = {
                        current?.let {
                            val intent = Intent(Intent.ACTION_SEND).apply {
                                type = "text/plain"
                                putExtra(Intent.EXTRA_TEXT, "${it.title} - ${it.artist}")
                            }
                            context.startActivity(Intent.createChooser(intent, "\u5206\u4eab\u6b4c\u66f2"))
                        }
                    }
                )
                if (state.showQueue) {
                    QueueSheet(
                        state = state,
                        onDismiss = { viewModel.showQueue(false) },
                        onPlay = viewModel::play,
                        onDownload = { song ->
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(viewModel.downloadUrl(song))))
                        },
                        onFavorite = viewModel::toggleFavorite,
                        onQueueType = viewModel::setQueueType
                    )
                }
                if (state.showSearch) {
                    SearchDialog(
                        state = state,
                        onChange = viewModel::updateSearchQuery,
                        onSubmit = viewModel::submitSearch,
                        onPlay = viewModel::play,
                        onDismiss = { viewModel.showSearch(false) }
                    )
                }
                if (state.showSettings) {
                    SettingsDialog(
                        config = state.config,
                        loading = state.loading,
                        message = state.updateMessage.ifBlank { state.message },
                        onChange = viewModel::updateConfig,
                        onSave = viewModel::saveAndConnect,
                        onRefresh = viewModel::loadSongs,
                        onCheckUpdate = { viewModel.checkUpdate(silent = false) },
                        onDismiss = { viewModel.showSettings(false) }
                    )
                }
                if (state.showActivation || (state.trialExpired && !state.licenseActive)) {
                    ActivationDialog(
                        state = state,
                        onChange = viewModel::updateLicenseInput,
                        onActivate = viewModel::activateLicense,
                        onDismiss = { viewModel.showActivation(false) }
                    )
                }
                if (state.showUpdateDialog && updateInfo != null) {
                    UpdateDialog(
                        info = updateInfo,
                        onDismiss = viewModel::dismissUpdate,
                        onDownload = {
                            val url = updateInfo.downloadUrl
                            if (url.isNotBlank()) {
                                context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                            }
                            viewModel.dismissUpdate()
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun PlayerScreen(
    state: MusicUiState,
    onTogglePlay: () -> Unit,
    onPrevious: () -> Unit,
    onNext: () -> Unit,
    onSeek: (Float) -> Unit,
    onCycleMode: () -> Unit,
    onQueue: () -> Unit,
    onSettings: () -> Unit,
    onActivation: () -> Unit,
    onRefresh: () -> Unit,
    onFavorite: () -> Unit,
    onSearch: () -> Unit,
    onShare: () -> Unit
) {
    val song = state.currentSong
    val title = song?.title?.ifBlank { stringResource(R.string.unknown_song) } ?: stringResource(R.string.app_name)
    val artist = song?.artist?.ifBlank { stringResource(R.string.unknown_artist) } ?: stringResource(R.string.open_and_listen)
    val progress = if (state.durationMs > 0L) (state.positionMs.toFloat() / state.durationMs).coerceIn(0f, 1f) else 0f

    Column(
        modifier = Modifier.fillMaxSize().padding(horizontal = 28.dp, vertical = 22.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onQueue) {
                Icon(Icons.Default.ArrowDropDown, contentDescription = "\u6253\u5f00\u64ad\u653e\u5217\u8868", tint = Color(0xFF111827), modifier = Modifier.size(36.dp))
            }
            Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(title, fontSize = 20.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(artist, color = Color(0xFF7B8491), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            IconButton(onClick = onShare) {
                Icon(Icons.Default.Share, contentDescription = "\u5206\u4eab", tint = Color(0xFF111827), modifier = Modifier.size(30.dp))
            }
        }
        Spacer(Modifier.height(34.dp))
        ChangTingLogo()
        Spacer(Modifier.height(36.dp))
        Text(title, fontSize = 30.sp, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Text(artist, fontSize = 20.sp, color = Color(0xFF7B8491), maxLines = 1, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(22.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(34.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onFavorite, modifier = Modifier.size(58.dp)) {
                Icon(
                    if (state.currentIsFavorite) Icons.Default.Favorite else Icons.Default.FavoriteBorder,
                    contentDescription = "\u6536\u85cf",
                    tint = if (state.currentIsFavorite) Color(0xFFE11D48) else Color(0xFF7B8491),
                    modifier = Modifier.size(42.dp)
                )
            }
            ModeButton(playModeLabel(state.playMode), true, onCycleMode)
            IconButton(onClick = onSearch, modifier = Modifier.size(58.dp)) {
                Icon(Icons.Default.Search, contentDescription = "\u641c\u7d22", tint = Color(0xFF111827), modifier = Modifier.size(34.dp))
            }
        }
        Spacer(Modifier.height(24.dp))
        Slider(value = progress, onValueChange = onSeek, modifier = Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(formatTime(state.positionMs), color = Color(0xFF9AA3AF))
            Text(formatTime(state.durationMs), color = Color(0xFF9AA3AF))
        }
        Spacer(Modifier.height(34.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(34.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onPrevious, modifier = Modifier.size(58.dp)) {
                Icon(Icons.Default.SkipPrevious, contentDescription = "\u4e0a\u4e00\u9996", modifier = Modifier.size(44.dp), tint = Color(0xFF111827))
            }
            IconButton(
                onClick = onTogglePlay,
                modifier = Modifier.size(86.dp).clip(CircleShape).background(Color(0xFF111827))
            ) {
                Icon(if (state.isPlaying) Icons.Default.Pause else Icons.Default.PlayArrow, contentDescription = "\u64ad\u653e/\u6682\u505c", tint = Color.White, modifier = Modifier.size(54.dp))
            }
            IconButton(onClick = onNext, modifier = Modifier.size(58.dp)) {
                Icon(Icons.Default.SkipNext, contentDescription = "\u4e0b\u4e00\u9996", modifier = Modifier.size(44.dp), tint = Color(0xFF111827))
            }
        }
        Spacer(Modifier.weight(1f))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onSettings, modifier = Modifier.size(58.dp)) {
                Icon(Icons.Default.Settings, contentDescription = "\u8bbe\u7f6e", modifier = Modifier.size(34.dp), tint = Color(0xFF111827))
            }
            Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.weight(1f)) {
                Text(
                    if (state.licenseActive) "VIP\u7528\u6237" else "\u70b9\u6211\u6fc0\u6d3b",
                    color = if (state.licenseActive) Color(0xFF16A34A) else Color(0xFFE11D48),
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.clickable { onActivation() }
                )
                Text(if (state.loading) "\u6b63\u5728\u52a0\u8f7d..." else state.message, color = Color(0xFF64748B), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            IconButton(onClick = onRefresh, modifier = Modifier.size(58.dp)) {
                Icon(Icons.Default.Refresh, contentDescription = "\u5237\u65b0", modifier = Modifier.size(34.dp), tint = Color(0xFF111827))
            }
        }
    }
}

@Composable
private fun ChangTingLogo() {
    Box(contentAlignment = Alignment.Center, modifier = Modifier.size(252.dp)) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            drawCircle(Color(0xFFE9E4FF), radius = size.minDimension * 0.48f)
            drawCircle(Color.White.copy(alpha = 0.62f), radius = size.minDimension * 0.38f)
            drawCircle(
                brush = Brush.radialGradient(listOf(Color(0xFF1E3A8A), Color(0xFF4F7CFF), Color(0xFF23C4B8))),
                radius = size.minDimension * 0.26f
            )
            val cx = center.x
            val cy = center.y
            listOf(0.16f, 0.22f, 0.28f).forEachIndexed { index, radius ->
                drawCircle(
                    color = Color.White.copy(alpha = 0.22f - index * 0.04f),
                    radius = size.minDimension * radius,
                    style = androidx.compose.ui.graphics.drawscope.Stroke(width = 4.dp.toPx())
                )
            }
            drawLine(Color.White.copy(alpha = 0.75f), Offset(cx - 44.dp.toPx(), cy + 46.dp.toPx()), Offset(cx + 44.dp.toPx(), cy + 46.dp.toPx()), 5.dp.toPx(), StrokeCap.Round)
        }
        Text("\u7545\u542c", color = Color.White, fontSize = 40.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun ModeButton(text: String, selected: Boolean, onClick: () -> Unit) {
    val color = if (selected) Color(0xFF2563EB) else Color(0xFF7B8491)
    OutlinedButton(onClick = onClick, shape = RoundedCornerShape(999.dp)) {
        Text(text, color = color, fontSize = 13.sp)
    }
}

private fun playModeLabel(mode: PlayMode): String = when (mode) {
    PlayMode.Single -> "\u5355\u66f2\u5faa\u73af"
    PlayMode.Sequential -> "\u987a\u5e8f\u64ad\u653e"
    PlayMode.Random -> "\u968f\u673a\u64ad\u653e"
}

@Composable
private fun QueueTypeButton(text: String, selected: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Button(
        onClick = { if (!selected) onClick() },
        shape = RoundedCornerShape(999.dp),
        modifier = modifier,
        colors = ButtonDefaults.buttonColors(
            containerColor = if (selected) Color(0xFF6F4DB5) else Color(0xFFD9DEE7),
            contentColor = if (selected) Color.White else Color(0xFF7B8491)
        )
    ) {
        Text(text, maxLines = 1, fontSize = 13.sp)
    }
}

@Composable
private fun QueueSheet(
    state: MusicUiState,
    onDismiss: () -> Unit,
    onPlay: (Song) -> Unit,
    onDownload: (Song) -> Unit,
    onFavorite: (Song) -> Unit,
    onQueueType: (QueueType) -> Unit
) {
    val songs = state.activeSongs
    val listState = rememberLazyListState()
    val currentIndex = songs.indexOfFirst { it.id == state.currentSong?.id }
    LaunchedEffect(state.queueType, state.currentSong?.id, songs.size) {
        if (currentIndex >= 0) {
            listState.scrollToItem(currentIndex)
        }
    }
    Box(
        modifier = Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.55f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.BottomCenter
    ) {
        Card(
            modifier = Modifier.fillMaxWidth().fillMaxHeight(0.62f).clickable(enabled = false) {},
            shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0xFFF1F7FF))
        ) {
            Column(Modifier.padding(22.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("\u5f53\u524d\u64ad\u653e", fontSize = 26.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Default.Close, contentDescription = "\u5173\u95ed", modifier = Modifier.size(34.dp))
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    QueueTypeButton("\u5168\u90e8", state.queueType == QueueType.All, Modifier.weight(1f)) { onQueueType(QueueType.All) }
                    QueueTypeButton("\u6211\u7684\u6536\u85cf", state.queueType == QueueType.Favorites, Modifier.weight(1f)) { onQueueType(QueueType.Favorites) }
                    QueueTypeButton("\u6211\u7684\u641c\u7d22", state.queueType == QueueType.Searches, Modifier.weight(1f)) { onQueueType(QueueType.Searches) }
                }
                Spacer(Modifier.height(10.dp))
                if (songs.isEmpty()) {
                    Text(emptyQueueText(state.queueType), color = Color(0xFF7B8491), modifier = Modifier.padding(20.dp))
                } else {
                    LazyColumn(state = listState, verticalArrangement = Arrangement.spacedBy(14.dp)) {
                        itemsIndexed(songs, key = { _, song -> song.id }) { index, song ->
                            QueueRow(
                                index = index + 1,
                                song = song,
                                active = song.id == state.currentSong?.id,
                                favorite = song.id in state.favoriteIds,
                                onPlay = { onPlay(song) },
                                onDownload = { onDownload(song) },
                                onFavorite = { onFavorite(song) }
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun QueueRow(index: Int, song: Song, active: Boolean, favorite: Boolean, onPlay: () -> Unit, onDownload: () -> Unit, onFavorite: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(if (active) Color(0xFFE0F7FA) else Color.Transparent)
            .clickable(onClick = onPlay)
            .padding(vertical = 10.dp, horizontal = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(if (active) "\u266a" else index.toString(), color = if (active) Color(0xFF17A8B8) else Color(0xFF9AA3AF), fontSize = 22.sp, modifier = Modifier.width(54.dp), textAlign = TextAlign.Center)
        Column(Modifier.weight(1f)) {
            Text(song.title.ifBlank { stringResource(R.string.unknown_song) }, fontWeight = FontWeight.Bold, fontSize = 20.sp, color = if (active) Color(0xFF17A8B8) else Color(0xFF111827), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(song.artist.ifBlank { stringResource(R.string.unknown_artist) }, color = Color(0xFF7B8491), maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        IconButton(onClick = onFavorite) {
            Icon(if (favorite) Icons.Default.Favorite else Icons.Default.FavoriteBorder, contentDescription = "\u6536\u85cf", tint = if (favorite) Color(0xFFE11D48) else Color(0xFF7B8491))
        }
        IconButton(onClick = onDownload) {
            Icon(Icons.Default.Download, contentDescription = "\u4e0b\u8f7d", tint = Color(0xFF7B8491))
        }
    }
}

private fun emptyQueueText(type: QueueType): String = when (type) {
    QueueType.All -> "\u6682\u65e0\u6b4c\u66f2"
    QueueType.Favorites -> "\u6682\u65e0\u6536\u85cf\u6b4c\u66f2"
    QueueType.Searches -> "\u6682\u65e0\u641c\u7d22\u6b4c\u66f2"
}

@Composable
private fun SearchDialog(
    state: MusicUiState,
    onChange: (String) -> Unit,
    onSubmit: (String) -> Unit,
    onPlay: (Song) -> Unit,
    onDismiss: () -> Unit
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("\u641c\u7d22\u6b4c\u66f2") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = state.searchQuery,
                    onValueChange = onChange,
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("\u8f93\u5165\u6b4c\u540d\u6216\u6b4c\u624b") },
                    singleLine = true
                )
                if (state.searchMessage.isNotBlank()) {
                    Text(state.searchMessage, color = if (state.searchLoading) Color(0xFF2563EB) else Color(0xFF64748B))
                }
                if (state.searchSuggestions.isNotEmpty()) {
                    Text("\u66f2\u5e93\u8054\u60f3", color = Color(0xFF7B8491), fontSize = 13.sp)
                    LazyColumn(Modifier.fillMaxWidth().heightIn(max = 220.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        itemsIndexed(state.searchSuggestions, key = { _, song -> song.id }) { _, song ->
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(Color(0xFFEAF2FF))
                                    .clickable {
                                        onChange("${song.title} - ${song.artist}")
                                        onSubmit("${song.title} - ${song.artist}")
                                    }
                                    .padding(10.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Column(Modifier.weight(1f)) {
                                    Text(song.title.ifBlank { stringResource(R.string.unknown_song) }, fontWeight = FontWeight.Bold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                    Text(song.artist.ifBlank { stringResource(R.string.unknown_artist) }, color = Color(0xFF7B8491), maxLines = 1, overflow = TextOverflow.Ellipsis)
                                }
                                IconButton(onClick = { onPlay(song) }) {
                                    Icon(Icons.Default.PlayArrow, contentDescription = "\u64ad\u653e", tint = Color(0xFF2563EB))
                                }
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(onClick = { onSubmit(state.searchQuery) }, enabled = !state.searchLoading) {
                Text(if (state.searchLoading) "\u5904\u7406\u4e2d" else "\u641c\u7d22")
            }
        },
        dismissButton = {
            OutlinedButton(onClick = onDismiss) { Text("\u5173\u95ed") }
        }
    )
}

@Composable
private fun SettingsDialog(
    config: ServerConfig,
    loading: Boolean,
    message: String,
    onChange: (ServerConfig) -> Unit,
    onSave: () -> Unit,
    onRefresh: () -> Unit,
    onCheckUpdate: () -> Unit,
    onDismiss: () -> Unit
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("\u8bbe\u7f6e") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = config.baseUrl,
                    onValueChange = { onChange(config.copy(baseUrl = it)) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text(stringResource(R.string.server_address)) },
                    singleLine = true
                )
                OutlinedTextField(
                    value = config.username,
                    onValueChange = { onChange(config.copy(username = it)) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text(stringResource(R.string.username)) },
                    singleLine = true
                )
                OutlinedTextField(
                    value = config.password,
                    onValueChange = { onChange(config.copy(password = it)) },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text(stringResource(R.string.password)) },
                    visualTransformation = PasswordVisualTransformation(),
                    singleLine = true
                )
                Text("\u6fc0\u6d3b\u72b6\u6001\u53ef\u5728\u64ad\u653e\u9875\u5e95\u90e8\u67e5\u770b\uff0c\u8bd5\u7528\u5230\u671f\u540e\u9700\u8981\u8f93\u5165\u5e8f\u5217\u53f7\u3002", color = Color(0xFF7B8491))
                if (message.isNotBlank()) {
                    Text(message, color = Color(0xFF2563EB))
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedButton(onClick = onRefresh, enabled = !loading, modifier = Modifier.weight(1f)) {
                        Text(stringResource(R.string.load_songs), maxLines = 1)
                    }
                    OutlinedButton(onClick = onCheckUpdate, enabled = !loading, modifier = Modifier.weight(1f)) {
                        Text("\u68c0\u67e5\u66f4\u65b0", maxLines = 1)
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedButton(onClick = onDismiss, modifier = Modifier.weight(1f)) {
                        Text("\u5173\u95ed", maxLines = 1)
                    }
                    Button(onClick = onSave, enabled = !loading, modifier = Modifier.weight(1f)) {
                        Text(stringResource(R.string.save_and_connect), maxLines = 1)
                    }
                }
            }
        },
        confirmButton = {},
        dismissButton = {}
    )
}

@Composable
private fun ActivationDialog(
    state: MusicUiState,
    onChange: (String) -> Unit,
    onActivate: () -> Unit,
    onDismiss: () -> Unit
) {
    AlertDialog(
        onDismissRequest = { if (!state.trialExpired || state.licenseActive) onDismiss() },
        title = { Text("\u8f93\u5165\u5e8f\u5217\u53f7\u6fc0\u6d3b") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(
                    if (state.licenseActive) "\u6c38\u4e45VIP\u7528\u6237" else "\u672a\u6fc0\u6d3b\u7528\u6237",
                    color = if (state.licenseActive) Color(0xFF16A34A) else Color.Unspecified
                )
                Text("\u9ed8\u8ba4\u8bd5\u7528 3 \u5929\u3002\u5982\u679c\u6ca1\u6709\u5e8f\u5217\u53f7\uff0c3 \u5929\u540e\u5c06\u65e0\u6cd5\u4f7f\u7528\u3002")
                Text("\u8bf7\u52a0\u5fae\u4fe1\u53f7\uff1aABCD59113 \u83b7\u53d6\u5e8f\u5217\u53f7\u3002", fontWeight = FontWeight.Bold)
                OutlinedTextField(
                    value = state.licenseInput,
                    onValueChange = onChange,
                    label = { Text("\u516d\u4f4d\u5e8f\u5217\u53f7") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                if (state.licenseMessage.isNotBlank()) {
                    Text(if (state.licenseActive) "\u6c38\u4e45VIP\u7528\u6237" else state.licenseMessage, color = if (state.licenseActive) Color(0xFF16A34A) else Color(0xFFE11D48))
                }
            }
        },
        confirmButton = {
            if (!state.licenseActive) {
                Button(onClick = onActivate) { Text("\u7acb\u5373\u6fc0\u6d3b") }
            }
        },
        dismissButton = {
            if (!state.licenseActive && !state.trialExpired) {
                OutlinedButton(onClick = onDismiss) { Text("\u7a0d\u540e\u518d\u8bf4") }
            }
        }
    )
}

@Composable
private fun UpdateDialog(info: AppUpdateInfo, onDismiss: () -> Unit, onDownload: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("\u53d1\u73b0\u65b0\u7248\u672c") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("\u7248\u672c\uff1a${info.versionName}")
                if (info.releaseNotes.isNotBlank()) {
                    Text(info.releaseNotes)
                }
            }
        },
        confirmButton = {
            Button(onClick = onDownload) { Text("\u7acb\u5373\u66f4\u65b0") }
        },
        dismissButton = {
            OutlinedButton(onClick = onDismiss) { Text("\u7a0d\u540e\u518d\u8bf4") }
        }
    )
}

private fun formatTime(ms: Long): String {
    val total = (ms / 1000).coerceAtLeast(0)
    return "${total / 60}:${(total % 60).toString().padStart(2, '0')}"
}
