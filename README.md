# LectureFlow Local

[![CI](https://github.com/Chloiris/LectureFlow-Local/actions/workflows/ci.yml/badge.svg)](https://github.com/Chloiris/LectureFlow-Local/actions/workflows/ci.yml)

> **Alpha / research prototype.** 当前仓库记录的是已验证的纵向 MVP，不代表任意课程、完整多 P
> 批处理或生产环境已经完成。请先在短片段和副本目录中验证，再处理重要资料。

LectureFlow Local 是一个面向 macOS / Apple Silicon 的本地课程处理流水线。它把视频或
Bilibili 课程转换为可追溯的时间轴、关键帧、证据账本、Obsidian 笔记与本地阅读页面。

项目遵守两条硬边界：不调用外部大模型 API；原始字幕与原始媒体永不被覆盖。语义和视觉
理解由当前 Codex 会话在本地 Agent Packet 上完成。

## 当前可运行范围

M0–M5 的纵向 smoke 已实现：项目隔离环境、环境诊断、本地/Bilibili 来源识别、任务注册、元数据、公开
B站人工/AI 字幕、本地 JSON/SRT/VTT/TXT 导入、统一时间轴、质量报告、格式导出、缓存与
断点恢复、本地 MLX-Whisper、关键帧、多模态单 Packet/证据，以及 P1 前五分钟的段落级三栏
阅读器和临时 Obsidian Markdown 导出。M4B 另对 P6 前 25 分钟完成了 5 个串行 Packet 的公式、
术语、渐进式 PPT、合并、长页面与临时 Vault 压力测试。M5 冻结该基线，仅增量处理 25:00 至 55:00.33，并交付完整 P6 的两段媒体阅读器、统一笔记和临时 Vault。当前仍不是完整课程处理；尚未实现
Canvas、RAG、桌面应用或通用多分 P 批处理。准确进度见 `PROJECT_STATUS.md`。

## 环境要求与安装

- macOS / Apple Silicon（主要验收平台）。
- Python 3.11 或 3.12，由 [uv](https://docs.astral.sh/uv/) 管理；不要修改系统 Python。
- FFmpeg 与 FFprobe。涉及 Bilibili 元数据或媒体时还需要项目锁定的 `yt-dlp`。

```bash
git clone https://github.com/Chloiris/LectureFlow-Local.git
cd LectureFlow-Local
uv sync --dev --locked
uv run lectureflow doctor
```

需要本地 MLX-Whisper 时，显式安装可选依赖。模型权重不随仓库分发，也不会由普通命令静默下载：

```bash
uv sync --dev --extra asr-mlx --locked
```

## 快速开始

```bash
uv sync --dev --locked
uv run lectureflow doctor
uv run lectureflow subtitles import "/path/包含 空格/字幕.srt" --language zh-CN
uv run lectureflow subtitles fetch "BV..."
uv run lectureflow status <job-id>
uv run lectureflow resume <job-id>
uv run lectureflow run "/path/课程.mp4" --start 0 --end 300 --until frames_ready
```

Bilibili 元数据与公开字幕使用项目锁定的 `yt-dlp` 以及内置的有界 HTTP 客户端：

```bash
uv run lectureflow subtitles fetch "https://www.bilibili.com/video/BV...?p=1"
uv run lectureflow run "BV..." --until transcript_ready
```

这不会读取浏览器 Cookie。没有公开字幕时返回可恢复的结构化结果，不会自动下载 ASR 模型。
对单 P 可显式提供本地后备字幕：

```bash
uv run lectureflow subtitles fetch "BV...?p=1" \
  --local-subtitle "/path/本地 字幕.vtt" --local-language zh-CN
```

每个分 P 在 `transcript/parts/<part-id>/` 输出 `original.json`、原始/清理 JSONL、TXT、SRT、
VTT、来源/选择/质量报告。单 P 和本地字幕任务同时在 `transcript/` 提供便捷副本。无时间 TXT
保持 `untimed_transcript`，SRT/VTT 为空提示，不伪造时间戳。

## 可选 VibeVoice-ASR-BitNet 后端

LectureFlow 支持把微软官方 `VibeASR.cpp` 1.5B BitNet 运行时作为显式本地 ASR 选项。它不会成为
核心依赖，也不会自动克隆、编译或下载模型。默认仍为已验收的 `mlx-whisper`；已有 transcript 的
job 禁止切换后端覆盖 raw，模型对比必须注册独立 job。

本地运行时需包含官方 `asr_infer`，模型目录只需以下两个固定 GGUF（合计
1,695,957,664 字节），不要执行会同时下载 Safetensors 的整仓下载命令：

```text
vibeasr-vae-encoder-i8_s.gguf
vibeasr-lm-i2_s-embed-q6_k.gguf
```

在被 Git 忽略的本地配置中指定：

```toml
[asr]
vibeasr_executable = "/absolute/path/VibeASR.cpp/build/bin/asr_infer"
vibeasr_executable_sha256 = "<64-hex output from shasum -a 256 asr_infer>"
vibeasr_model_path = "/absolute/path/models/vibeasr"
vibeasr_threads = 4
vibeasr_chunk_seconds = 30.0
vibeasr_context_size = 16384
vibeasr_max_tokens = 4096
vibeasr_hotwords = ["Scaling Law", "State Space Model"]
```

然后先只读检查和 20–30 秒预检：

```bash
uv run lectureflow asr doctor --backend vibeasr-bitnet
uv run lectureflow asr preflight <job-id> --backend vibeasr-bitnet --seconds 30
uv run lectureflow asr transcribe <job-id> --backend vibeasr-bitnet \
  --start 0 --end 300 --offline
```

官方 1.5B BitNet 只保证纯文本输出，不提供 7B 版本的模型时间戳或 speaker。LectureFlow 因而使用
独立 24 kHz mono PCM，将音频确定性切成最多 30 秒的块；每条 segment 的 start/end 是音频块边界，
不是模型对齐结果，`speaker` 与 `confidence` 均为 null。内容寻址原始 stdout、二进制/模型 SHA-256
与热词哈希进入审计记录。运行时显式传递 context size，并在输入预算溢出、decode/prefill 错误或
达到输出 token 上限时失败，不接受可能截断的转写，也不调用云端服务。

M2 命令：

```bash
uv run lectureflow media acquire <job-id>
uv run lectureflow media inspect <job-id>
uv run lectureflow frames extract <job-id>
uv run lectureflow frames inspect <job-id>
uv run lectureflow frames contact-sheet <job-id>
```

B站媒体限制 1080p，预计超过 500 MiB 时不会下载。根多 P URL 默认处理 P1；其他 P 使用
`?p=N`。默认最终帧最多 30 张，联系表每页最多 12 张。M2 完全由 FFmpeg/Pillow 确定性处理，
不会调用模型视觉理解或 OCR。

M4A 段落级 MVP 命令（要求任务已有 M3B 产物）：

```bash
uv run lectureflow paragraphs build <job-id>
uv run lectureflow corrections build <job-id>
uv run lectureflow render <job-id>
uv run lectureflow serve <job-id> --host 127.0.0.1 --port 8765
uv run lectureflow export-obsidian <job-id> --vault ".tmp/m4a-obsidian-smoke"
```

页面跳转和播放高亮以 ASR 段落为粒度，不提供逐词/逐句对齐。raw、cleaned、corrected 分层保存；
只有有明确 transcript/frame 证据的 accepted 纠错才应用。Web 无 CDN且只监听 loopback；M4A
导出不包含 Canvas、视频或模型。

M4B P6 压力测试命令（复用已缓存 medium MLX 模型）：

> 以下 M4B/M5 命令是固定 Bilibili 公开视频上的可复现工程基准，不是通用整课入口，也不会随
> 普通安装附带媒体、模型、字幕、帧或分析结果。

```bash
uv run lectureflow run "https://www.bilibili.com/video/BV1pf421z757?p=6" \
  --start 0 --end 1500 --until transcript_ready
uv run lectureflow frames extract <job-id> --config config/m4b-p6.toml
uv run lectureflow paragraphs build <job-id>
uv run lectureflow packets build <job-id>
uv run lectureflow packets validate <job-id>
uv run lectureflow analyze-packets <job-id>
uv run lectureflow merge <job-id>
uv run lectureflow evidence validate <job-id>
uv run lectureflow render <job-id>
```

真实分析由当前 Codex 串行读取入选高清帧后写入；命令本身不会启动 `codex exec`、外部模型 API
或第二个 ASR 模型。默认页面仍只监听 `127.0.0.1`，内部 Packet overlap 不出现在正文。

M5 完整 P6 增量命令：

```bash
uv run lectureflow p6 plan "https://www.bilibili.com/video/BV1pf421z757?p=6"
uv run lectureflow freeze <m4c-job-id> --range 0:1500
uv run lectureflow run "https://www.bilibili.com/video/BV1pf421z757?p=6" \
  --start 1500 --end 3300.33 --until transcript_ready
uv run lectureflow p6 materialize-transcript <incremental-job-id> --frozen-job <m4c-job-id>
uv run lectureflow frames canonicalize <incremental-job-id> --frozen-job <m4c-job-id>
uv run lectureflow p6 merge --frozen-job <m4c-job-id> --incremental-job <incremental-job-id>
uv run lectureflow serve <incremental-job-id> --host 127.0.0.1 --port 8765
```

## 开发验证

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

运行时数据写入 `workspaces/`，该目录默认被 Git 忽略。

## 文档、安全与许可

- [架构](ARCHITECTURE.md) · [数据 Schema](docs/DATA_SCHEMA.md) ·
  [流水线](docs/PIPELINE.md) · [隐私边界](docs/PRIVACY.md) ·
  [故障排查](docs/TROUBLESHOOTING.md)
- 安全问题请按 [SECURITY.md](SECURITY.md) 私下报告；贡献前请阅读
  [CONTRIBUTING.md](CONTRIBUTING.md)。第三方组件边界见
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 本仓库不包含课程媒体、模型权重、真实任务工作区或用户笔记。使用者有责任确认自己有权下载、
  转写、截图和保存来源内容，并遵守来源平台条款与当地法律。
- 本项目与 Bilibili、Microsoft、OpenAI 及文档中提及的参考项目均无隶属或背书关系。
- 仓库公开可见不等于开源授权。当前代码保持专有，适用根目录 [LICENSE](LICENSE)；未经另行
  书面许可，不授予复制、修改或再分发权利。
