<p align="center">
  <img src="docs/images/logo.svg" width="108" alt="畅听 Logo" />
</p>

<h1 align="center">畅听 · 自建音乐库</h1>

<p align="center">自研音频播放器：FastAPI 后台 + Navidrome 串流 + 安卓客户端 + AI 音乐生成</p>

## 界面预览

<p align="center">
  <img src="docs/images/screenshot-player.png" width="280" alt="播放界面" />
  &nbsp;&nbsp;
  <img src="docs/images/screenshot-queue.png" width="280" alt="播放列表" />
</p>

> 界面图基于 `android/` 中 Compose 代码还原绘制，实际效果以真机为准。

## 系统架构

```mermaid
flowchart LR
    subgraph 客户端
        A["畅听 App<br/>Android · Jetpack Compose"]
    end

    subgraph 服务端
        B["backend<br/>FastAPI · SQLite"]
        N["Navidrome<br/>音乐串流"]
        BE["beets<br/>音频导入/刮削"]
        G["ai_generator<br/>AI 音乐生成"]
    end

    L["Last.fm API<br/>热门榜单"]
    I["inbox<br/>合法音频文件"]

    A -- "Subsonic API 播放/收藏" --> N
    A -- "REST · 榜单/搜索/APK 更新" --> B
    B -- "生成任务/结果回传" --> G
    B -- "同步热门榜单" --> L
    I --> BE --> N
```

代码分为三部分：

- `backend`：后台服务、Last.fm 热门榜单同步、合法音频导入、Navidrome Docker 配置、beets 配置。
- `android`：安卓音乐客户端，中文界面，连接 Navidrome 播放音乐。
- `ai_generator`：AI 音乐生成服务，供 `backend` 的 `ai_music` 路由调用。

## 推荐落地顺序

1. 本地运行 `backend`，确认 API 能打开。
2. 申请 Last.fm API Key，写入 `backend/.env`。
3. 把合法音频放入 `backend/data/inbox`。
4. 用后台接口或 beets 导入音乐。
5. 启动 Navidrome，创建管理员账号。
6. 安卓 App 填 Navidrome 地址和账号。

## 环境建议

- 后台：Python 3.12 或 Docker。
- 安卓：Android Studio Koala 或更新版本、JDK 17、Android SDK 35。

## 重要说明

本项目不会实现自动下载版权音乐。热门榜单只作为发现和参考数据；真正入库的音频必须来自合法来源。
