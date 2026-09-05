# Troubleshooting

## Python 版本过低

macOS 的 `/usr/bin/python3` 可能是 3.9。不要升级或替换系统 Python；使用：

```bash
uv sync --dev
uv run lectureflow doctor
```

## 找不到 yt-dlp

Bilibili 元数据和字幕阶段需要项目锁定的依赖。恢复项目隔离环境：

```bash
uv sync --dev
```

无需登录的视频不需要 Cookie。不要把 `cookies.txt` 放进仓库。

## 阶段失败

先运行 `lectureflow status <job-id>` 查看失败阶段和恢复提示，再运行
`lectureflow resume <job-id>`。不要删除任务目录；尝试历史用于审计和恢复。

## FFmpeg/FFprobe 缺失

`lectureflow doctor` 会只读报告缺失项。系统级安装需要用户自行确认后执行，LectureFlow 不会
自动修改全局环境。

## 没有公开字幕

`subtitles fetch` 返回 `no_public_subtitles` 时，任务保留为可恢复状态，且不会下载 ASR 模型。
可对显式单 P 提供本地后备字幕，或之后配置本地 ASR：

```bash
uv run lectureflow subtitles fetch "BV...?p=1" --local-subtitle "/path/字幕.srt"
```

查看 `subtitle-result.json` 的逐 P 分类：D 是接口明确无字幕，E 是需要登录。LectureFlow 不会
把 E 当成 D，也不会自动读取浏览器 Cookie。

## 严格离线字幕缓存

```bash
uv run lectureflow subtitles fetch "<B站URL>" --offline
```

该命令只读取已验证缓存。出现 `offline_cache_miss` 时先在允许联网的环境正常运行一次；离线
模式不会回退到网络。`--offline` 与 `--force-stage subtitle_ready` 互斥。

## 本地 TXT 没有 SRT cue

这是预期行为。普通 TXT 被保存为 `untimed_transcript`；在没有用户明确分配策略时，生成的
SRT 为空、VTT 只有文件头，避免伪造精确时间戳。

## 字幕产物完整性失败

如果 `status`/`export` 报告原始字幕或 artifact manifest 哈希不匹配，运行
`lectureflow resume <job-id>`。系统会从最小字幕阶段重建派生物，不删除内容哈希命名的旧原始
材料。不要手工修改 `pipeline-state.json`、`job-identity.json` 或 `subtitle-result.json`。

## ASR 后端或模型缺失

M3B 使用 uv 可选组和固定模型；不要替换系统 Python：

```bash
uv sync --dev --extra asr-mlx --locked
uv run lectureflow asr doctor
```

`asr doctor` 是严格离线检查。若报告 model cache miss，在线下载只能使用经过审计且低于 2 GiB
的固定 medium 模型；不要同时下载 small/Faster-Whisper。`asr transcribe --offline` 缺缓存时会
明确失败，绝不回退云端。

若 `T000015` 一类段落出现不合理的长文本/短时长或大空隙，查看
`asr-quality-report.json`，保留原始字幕并把问题记为人工时间戳复核；不要手改 original.jsonl。

## 媒体估算超过 500 MiB

LectureFlow 会在 B站下载前停止。优先缩短 `--start/--end`；确实需要更大文件时，应在新的请求
中说明来源、估算大小和用途并取得用户确认，不能绕过门禁。抽帧配置变化只应重跑 frames，
不应重新下载媒体。

## 多 P 课程抽错分 P

根多 P URL 在 M2 媒体阶段明确默认选择 P1，`media inspect` 可查看 `selected_part` 和
`part_selection_reason`。要处理其他 P，请创建显式 `?p=N` 的独立任务；不要手改 manifest。

## frames_ready 失败或中断

```bash
uv run lectureflow status <job-id>
uv run lectureflow resume <job-id>
uv run lectureflow frames inspect <job-id>
```

运行中断会把旧 running attempt 标为 recoverable 并追加新尝试。单张最终帧损坏时自动局部
修复；报告、候选账本或多个关键产物损坏时重跑整个 `frames_ready`。不要删除 media 目录。

## 提示词候选为 0

先确认任务是否有 timed transcript。无公开字幕且未配置本地字幕/ASR 时，M2 会如实记录
`cue_candidates: 0`，仍执行场景变化和周期采样；不会伪造提示词命中或自动下载 ASR 模型。

## 最终帧很少或最大时间缺口较大

先看 `frame-candidates.jsonl` 和 `frame-dedup-report.json`。静态 PPT 的周期帧可能与更清晰的
场景帧全局去重，原采样时间仍在候选账本和去重映射中。若确有遗漏，可用配置降低 scene
阈值、缩短 periodic 间隔或降低 duplicate threshold，然后只强制 `frames_ready`。

## M4A 段落或纠错校验失败

先运行 `paragraphs build` 和 `corrections build`，不要手改原始 ASR。段落错误会指出遗漏、重复、
非连续 segment 或边界不匹配；修正 `analyses/m4a/paragraph-plan.json` 后只重建 M4A。纠错缺少
frame/transcript 证据时必须保持 pending，不能通过改正文绕过校验。

## 本地阅读器无法启动或视频不能 seek

服务只允许 `127.0.0.1`。端口占用时更换 `--port` 或停止已有 LectureFlow 服务；不要改为
`0.0.0.0`。视频 seek 依赖 Range；若返回 404，先用 `status` 复核媒体 manifest 与文件哈希。
页面不加载远程脚本，断网仍应可用。

## Obsidian smoke 导出拒绝覆盖

导出器只管理带 LectureFlow 标记且记录在 `.lectureflow-export.json` 的文件。若目标中有用户文件
或上次生成物被手工编辑，会明确拒绝覆盖；请改用新的临时 Vault，或先保留手工修改后显式选择
新的输出目录。M4A 不写真实 Vault、不生成 Canvas、也不复制视频或模型。

## M4B Packet、公式或渐进式帧校验失败

先运行 `packets validate` 并检查对应 `packet-validation.json`，不要重跑 ASR。overlap 可以重复
出现在 context，但 primary transcript、最终段落与 evidence 不得重复。只修受影响 Packet 的
结构化分析，再重跑 `merge` 和下游。

公式不清楚时保留原帧或 crop，令 LaTeX 为空并标 `needs_review`，不要根据教材常识补写。渐进式
页面疑似被合并时比较 `progressive-slide-report.json` 的局部/边缘/高对比文本变化；必要时只重跑
frames/Packet，不触发媒体或 ASR。

## 缓存恢复保留旧中断 attempt

阶段尝试是追加审计记录。若进程被中断但 immutable artifact 仍与 source、分 P、range、工具及
SHA 匹配，下一次运行追加 `*-cache-restore` 成功 attempt；旧 interrupted attempt 继续保留。
内容 SHA 不匹配时必须正常重建，不能用恢复标记绕过完整性校验。

## 完整 P6 在 25:00 切换媒体

M5 默认使用两段媒体。页面显示完整 P6 绝对时间，原生播放器内部时间在第二段从 0 开始属于
正常实现；段落、章节、公式和搜索跳转会自动减去 1500 秒并切换 `/media/segment/1`。若无法
seek，检查两个 endpoint 是否都返回 `Accept-Ranges: bytes`，不要重新运行 ASR。

若音频文件在一次可恢复失败后已通过完整哈希和 FFprobe 校验，下一次严格离线运行会追加
`ffmpeg-cache-adoption` 成功尝试，不重新抽取音频，也不会把当前里程碑退回 `audio_ready`。

## VibeASR BitNet 显示 unavailable

先运行 `lectureflow asr doctor --backend vibeasr-bitnet --json`。该后端不会自动安装：必须配置可执行
的官方 `asr_infer`，并让 `vibeasr_model_path` 同时包含两份固定 GGUF。LectureFlow 会核对固定大小
与 SHA-256；不要用未过滤的 Hugging Face 整仓下载，因为同一仓库还含超过 10 GiB 的 Safetensors。
strict offline 缺文件会直接失败，不会回退云端或改用另一模型。

若 job 已有 MLX transcript，切换到 VibeASR 会得到 immutable transcript 错误。这是防覆盖门禁；
请用相同来源/range 的独立 benchmark job 比较，不要删除或强制覆盖原始转写。

若只有 cleaned/SRT/VTT/quality 等派生 ASR artifact 损坏，当前版本也会先 fail-closed，不会借
`--force-stage` 覆盖 raw/original。自动的“只从 immutable original 重建派生文件”尚未实现；在
该命令完成前应保留任务目录并使用独立 workspace，而不是手工删除原始文件。
