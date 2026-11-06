package com.zijiemusic.app

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.zijiemusic.app.ui.MusicApp
import com.zijiemusic.app.ui.MusicViewModel

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        volumeControlStream = AudioManager.STREAM_MUSIC
        requestNotificationPermission()
        showPlaybackPermissionGuide()
        setContent {
            val viewModel: MusicViewModel = viewModel()
            MusicApp(viewModel)
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
        }
    }

    private fun showPlaybackPermissionGuide() {
        val prefs = getSharedPreferences("permission_guide", MODE_PRIVATE)
        if (prefs.getBoolean("shown", false)) return
        prefs.edit().putBoolean("shown", true).apply()
        AlertDialog.Builder(this)
            .setTitle("\u540e\u53f0\u64ad\u653e\u6388\u6743")
            .setMessage("\u4e3a\u4e86\u606f\u5c4f\u540e\u7ee7\u7eed\u64ad\u653e\uff0c\u5e76\u5728\u9501\u5c4f\u754c\u9762\u663e\u793a\u64ad\u653e\u3001\u6682\u505c\u3001\u4e0a\u4e00\u9996\u3001\u4e0b\u4e00\u9996\uff0c\u8bf7\u5141\u8bb8\u901a\u77e5\uff0c\u5e76\u5728\u7cfb\u7edf\u8bbe\u7f6e\u91cc\u628a\u7545\u542c\u8bbe\u4e3a\u4e0d\u9650\u5236\u7535\u91cf\u3002\u90e8\u5206\u624b\u673a\u8fd8\u9700\u8981\u5141\u8bb8\u540e\u53f0\u5f39\u51fa\u754c\u9762\u6216\u60ac\u6d6e\u7a97\u3002")
            .setPositiveButton("\u53bb\u8bbe\u7f6e") { _, _ -> openBatteryOptimizationSettings() }
            .setNegativeButton("\u7a0d\u540e\u518d\u8bf4", null)
            .show()
    }

    private fun openBatteryOptimizationSettings() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val powerManager = getSystemService(PowerManager::class.java)
            if (powerManager?.isIgnoringBatteryOptimizations(packageName) != true) {
                runCatching {
                    startActivity(
                        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                            data = Uri.parse("package:$packageName")
                        }
                    )
                }.onFailure {
                    openAppSettings()
                }
                return
            }
        }
        openOverlaySettings()
    }

    private fun openOverlaySettings() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
            runCatching {
                startActivity(
                    Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION).apply {
                        data = Uri.parse("package:$packageName")
                    }
                )
            }.onFailure {
                openAppSettings()
            }
            return
        }
        openAppSettings()
    }

    private fun openAppSettings() {
        startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:$packageName")
            }
        )
    }
}
