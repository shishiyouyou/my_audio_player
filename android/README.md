# 自建音乐库安卓客户端

这是独立 Android 工程，界面默认中文。

当前功能：

- 保存 Navidrome 服务器地址、用户名、密码。
- 调用 Navidrome/Subsonic API 获取随机歌曲列表。
- 点击“播放”后使用系统音频播放器打开 Navidrome 流媒体地址。

## 开发环境

- Android Studio Koala 或更新版本。
- JDK 17。
- Android SDK 35。

## 运行

1. 用 Android Studio 打开 `F:\py_workspace_vscode\300.自建音乐库\code\android`。
2. 等待 Gradle 同步完成。
3. 运行 `app` 到模拟器或真机。
4. 在 App 中填写 Navidrome 地址，例如 `http://服务器IP:4533`。

如果命令行构建提示缺少 `gradle-wrapper.jar`，在 Android Studio 中执行一次 Gradle 同步，或安装本机 Gradle 后运行 `gradle wrapper --gradle-version 8.7`。

## 后续可扩展

- 内置 ExoPlayer 播放器。
- 播放队列、暂停/上一首/下一首。
- 收藏、搜索、专辑和歌手页。
- 离线缓存。
