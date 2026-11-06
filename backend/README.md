# 自建音乐库后台

这是后台工程，负责：

- 每天自动从 Last.fm 同步热门音乐榜单元数据。
- 把你拥有合法来源的音频文件从 `data/inbox` 导入到 `data/music`。
- 通过 Docker Compose 启动 Navidrome，让安卓客户端播放 `data/music` 中的音乐。

后台不会从互联网自动下载版权音乐。你需要只导入自己购买、拥有授权或开放许可的音频。

## 本地运行

建议使用 Python 3.12。当前依赖生态对 Python 3.14 的支持还不稳定，生产部署建议优先使用 Docker。

```powershell
cd F:\py_workspace_vscode\300.自建音乐库\code\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，填写 `LASTFM_API_KEY`。

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

接口文档：

- 后台 API：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health

## Docker 运行

```powershell
cd F:\py_workspace_vscode\300.自建音乐库\code\backend
Copy-Item .env.example .env
docker compose up -d --build
```

服务地址：

- 后台：http://服务器IP:8000
- Navidrome：http://服务器IP:4533

第一次打开 Navidrome 会要求创建管理员账号。

## Systemd 运行

如果服务器拉 Docker 镜像不稳定，可以使用 systemd 部署：

```bash
cd /home/music/code/backend
bash scripts/install_systemd.sh
bash scripts/install_navidrome_systemd.sh
```

## 使用流程

1. 把合法音频文件放入 `data/inbox`。
2. 调用 `POST /api/library/import`，后台会复制到 `data/music/歌手/专辑/歌曲名.ext`。
3. Navidrome 会定时扫描 `data/music`。
4. 安卓 App 填写 Navidrome 地址、用户名、密码后连接播放。
5. `POST /api/trending/sync` 可立即同步 Last.fm 热门榜单元数据。

## 使用 beets 整理音乐

如果你希望更严格地匹配专辑、封面和标签，可以运行：

```powershell
cd F:\py_workspace_vscode\300.自建音乐库\code\backend
.\.venv\Scripts\Activate.ps1
.\scripts\beets_import.ps1
```

beets 会读取 `beets-config.yaml`，把 `data/inbox` 中的音乐整理到 `data/music`。
