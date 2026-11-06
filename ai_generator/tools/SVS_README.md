# Melody-Conditioned SVS 接入准备

## 目标

把“保留原曲调，只更换歌词”从当前的风格参考生成，升级为更严格的歌声合成流程：

1. 从原曲或已有 LRC 提取句级时间轴。
2. 用人声分离和音高提取获得每句旋律、时长、停顿和节奏密度。
3. 按原曲每句字数、押韵位置、段落结构生成新歌词。
4. 用 melody-conditioned SVS 生成新 vocal。
5. 新 vocal 与原伴奏混音。
6. 用 ASR 和 F0 对齐做质量检查，不合格不入库。

## 本机工作区

```text
D:\music_svs
  .venv\
  models\
    OpenVPI-DiffSinger\
    RVC\
  runtime\
    OpenVPI-DiffSinger\
  outputs\
  logs\
```

## 当前运行时

已采用 OpenVPI 维护版 DiffSinger，而不是 2022 年原始 DiffSinger。原因：

- OpenVPI 版推荐 PyTorch >= 2.4，更适合当前 CUDA PyTorch 环境。
- 原始 DiffSinger 依赖 Python 3.8 / Torch 1.9，和当前环境差距较大。
- OpenVPI 版更接近生产部署，支持更高采样率和更强控制参数。

## 环境检查

在 `ai_generator` 目录执行：

```powershell
.\tools\check_svs_env.ps1
```

或直接执行：

```powershell
python .\tools\svs_env_check.py D:\music_svs
```

## 当前最低要求

- GPU：NVIDIA RTX 4070 Ti 可用。
- 空闲显存：建议 8GB 以上，推理前最好关闭 Comfy Desktop 或其他占 GPU 的程序。
- 磁盘：`D:\music_svs` 建议 60GB 以上空闲。
- Python：使用独立环境 `D:\music_svs\.venv`。
- PyTorch：必须是 CUDA 版。
- 运行时：`D:\music_svs\runtime\OpenVPI-DiffSinger`。
- 模型：还需要可用的 OpenVPI DiffSinger checkpoint，放到 `D:\music_svs\models\OpenVPI-DiffSinger`。

## 状态说明

`/health` 会返回 `melodySvs` 字段。

- `runtime.openvpiDiffSinger=true`：运行时代码已存在。
- `runtime.inferCli=true`：推理入口可启动。
- `models.diffSinger=true`：已有可用 checkpoint。
- `ready=true`：GPU、CUDA PyTorch、ffmpeg、运行时、checkpoint 都满足。

在 `ready=false` 时，线上系统仍使用当前 ACE-Step/Comfy 链路，不影响现有功能。
