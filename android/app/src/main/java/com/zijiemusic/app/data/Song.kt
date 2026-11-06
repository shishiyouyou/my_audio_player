package com.zijiemusic.app.data

data class Song(
    val id: String,
    val title: String,
    val artist: String,
    val album: String,
    val durationSeconds: Int,
    val streamUrl: String = "",
    val downloadUrl: String = ""
)
