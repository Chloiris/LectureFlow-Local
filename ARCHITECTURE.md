# Architecture

## 三层边界

1. 确定性本地工具层：来源识别、元数据、字幕、ASR、统一时间轴、抽帧、缓存、状态、导出和
   本地服务。该层不能依赖语言模型。
2. Codex Agent 理解层：读取 5–10 分钟 Packet 的字幕、联系表与入选高清帧，输出经 JSON
   Schema 验证的章节、概念、公式、视觉发现和不确定项。默认由当前交互会话执行。
3. 展示与知识库层：从验证后的证据数据渲染 Obsidian Markdown、Canvas、Mermaid 和只绑定
   `127.0.0.1` 的本地阅读器。

## 数据流

```text
source → manifest/metadata → subtitles or local ASR → immutable transcript
       → candidate frames/dedup/contact sheets → Agent Packets
       → validated analyses → evidence ledger/coverage → notes
       → Obsidian + local reader
```

权威中间数据使用带 `schema_version` 的 JSON/JSONL。Markdown、SRT、VTT、Canvas 与网页均是
可重建的展示物。

## 模块边界

- `sources/`：来源分类、Bilibili URL 规范化、本地只读引用和元数据获取。
- `network.py`：无 Cookie 的 HTTPS GET、有界重试、响应缓存、URL/错误脱敏。
- `schemas/`：持久化模型与版本边界。
- `state.py`：状态迁移、尝试历史、原子写入与任务锁。
- `pipeline.py`：阶段编排、输入哈希、缓存命中与恢复。
- `subtitles/`、`asr/`、`frames/`、`packets/`：确定性数据准备。
- `media/`：本地只读媒体解析、B站受限范围下载、大小门禁与完整性清单。
- `renderers/`、`obsidian/`、`web/`：仅消费已验证的数据。

## 状态机

里程碑顺序为：

```text
created → metadata_ready → subtitle_ready → audio_ready → transcript_ready
→ frames_ready → packets_ready → analysis_ready → evidence_ready → notes_ready
→ obsidian_ready → web_ready → complete
```

`failed` 是整体/阶段执行状态，不是会抹去进度的里程碑。阶段执行状态为 `pending`、
`running`、`succeeded`、`failed`、`invalidated` 或 `skipped`。每次尝试追加记录，不覆盖历史。

状态 JSON 通过同目录临时文件、`fsync` 和 `os.replace` 原子提交；每个任务用 `flock` 避免
并发写入。运行中断后保留最后成功阶段，由 `resume` 显式恢复。

## 缓存策略

阶段输入哈希由来源身份、分 P、时间范围、文件指纹、相关配置、工具/模型、提示词版本和
Schema 版本构成。相同输入且产物哈希匹配时跳过；输入变化只使目标阶段及后代失效，不删除
旧材料。`--force-stage` 同样只失效指定范围，不默认做破坏性清理。

M1 字幕缓存键包含来源/分 P/时间范围、yt-dlp 元数据哈希、语言优先级、来源优先级、本地后备
字幕指纹、配置与 Schema 版本。缓存命中还必须同时通过：`subtitle-result.json` 的阶段哈希、
每个分 P 的 artifact manifest、原始字幕路径边界和原始字幕 SHA-256。原始内容以内容哈希
命名，来源内容变化会保留旧版本而非覆盖。

M2 媒体缓存与抽帧缓存相互分离。媒体键只包含来源/分 P/时间范围、来源指纹或元数据哈希、
格式选择器以及 FFprobe/yt-dlp 版本；修改抽帧阈值不会重复下载。抽帧键包含媒体清单哈希、
字幕输入哈希、处理范围、完整 `frames` 配置和 Schema 版本。最终帧或联系表损坏时先按 artifact
manifest 定位；单个最终帧可按原时间点局部修复，其他损坏才重跑 `frames_ready`。

## 字幕与统一时间轴

`sources/bilibili_subtitles.py` 枚举公开分 P 和字幕候选，严格选择 uploader → Bilibili AI →
显式本地后备。字幕下载域限制为 Bilibili/HDslb/Bilivideo 的 HTTPS 域；不发送 Cookie。
`subtitles/formats.py` 解析 B站 JSON、SRT、VTT 和 untimed TXT，只做确定性 Unicode、空白和
标点清理及相邻重复去除。任务内 segment ID 按分 P 顺序全局编号，各分 P 时间轴独立且保留
原始秒数。

M3B 实现 `MLXWhisperBackend` 的可用性、描述和转写接口。项目 Python 3.12 环境使用 MLX/Metal，
模型固定为一个低于 2 GiB 的多语言 medium snapshot。媒体先确定性抽成 16 kHz 单声道 PCM；
音频缓存键绑定媒体 SHA、FFmpeg 和音频参数，转写键绑定音频 SHA、模型/revision、语言、任务和
Schema。严格 offline 只访问本地 Hugging Face 缓存；Faster-Whisper 仍只有能力检测。

`VibeASRBitNetBackend` 是显式启用的可选外部二进制适配器，不改变默认 ASR 优先级。它只接受
固定 revision 的微软官方 VibeASR.cpp 和两份固定 GGUF，使用 24 kHz mono PCM、argv subprocess
及官方 1.5B 的 `--prompt-format text`。由于 BitNet 不提供 7B 的 JSON 时间戳/说话人输出，本地层
按最多 30 秒音频块生成粗粒度 segment，并明确标记 chunk-boundary 时间来源、speaker=null。
缓存键额外包含二进制 SHA、两份模型 SHA、线程数、chunk/context/max tokens 和热词哈希；
strict offline 从不下载或构建。
同一 job 的 backend identity 不可变，后端对比必须使用独立 job，防止覆盖原始 transcript。

## M5 冻结与增量合并

M5 把 P6 拆成不可变冻结源 `0–1500` 与追加源 `1500–3300.33`。冻结清单保存 artifact 哈希和各 ID 最大值；新增 ID 按 `max+1` 追加。`merged/p6-complete/` 只引用两侧权威数据并生成统一时间轴、章节、证据、笔记和展示物，不回写上游。

播放器通过 `media-segments.json` 把两个媒体文件映射到一个绝对 P6 时间轴。Frame canonicalization 仅在 overlay 中把安全重复标为 inactive，文件与旧 ID 永久保留；阅读器展示 active frame，证据解析仍接受 canonical 与 legacy ID。

## M2 媒体与帧

本地 MP4/MOV/MKV/WebM 不复制到任务目录，`media-manifest.json` 只保存非敏感来源标识和完整
文件哈希；运行时从受保护 job manifest 解析原路径。B站媒体下载在任务目录，限制到 1080p，
通过项目解释器的 `python -m yt_dlp` 使用请求时间范围；估算超过 500 MiB 时在下载前失败。
根多 P URL 默认选 P1，显式页码优先，
两者均记录 `selected_part` 与 `part_selection_reason`。

候选帧由三个独立来源合并：FFmpeg `scene` 分数、字幕提示词与配置化前后偏移、周期安全采样。
FFmpeg 黑区间探针确保黑帧过滤行为本身可审计。Pillow 验证 PNG、计算亮度/标准差/锐度和 64 位
dHash；去重跨全局候选而非只比较相邻帧，按提示词、场景分数、清晰度优先保留并合并来源。
最终帧按时间排序，默认上限 30；联系表每页默认最多 12 张，只用于后续筛选。

## Agent Packet

本地脚本按时间和语义提示生成有限大小 Packet，附前后重叠上下文、帧清单、联系表和固定
输出 Schema。Codex 先看联系表筛选，再看高清原图做公式/代码/PPT 判断。每条输出必须引用
`segment_id` 或 `frame_id`；冲突和不清晰内容进入不确定项。验证失败的 Packet 不进入汇总。

M3B 只实现单一 `P0001`：0–300 秒字幕、7 帧 manifest、M3A 观察、联系表、媒体/音频清单、分析
Schema 和 artifact 哈希。当前 Codex 从中选择 4 张高清原图与联系表联合分析；分析通过 Schema、
provenance 和 ID 校验后才生成证据账本。证据分为 speech、slide、speech+slide，最后一种必须
同时引用两类 ID。该实现不是通用多 Packet 调度器。

## M4A 段落与纠错

M4A 直接消费 M3B 的 17 个 ASR segment、P0001 分析和证据账本，不重跑 ASR。当前 Codex 只提供
相邻 segment 分组、标题、整理稿和证据关联建议；`m4a/service.py` 确定性计算段落起止时间、验证
连续性和全量唯一覆盖，并生成 raw / cleaned / corrected 三层文字。纠错决策单独记录；只有同时
具备高置信 transcript 与 frame 证据的 accepted 项才应用，pending 保留但不改正文。

段落、纠错、章节和 Web 各自使用独立输入哈希。段落配置变化只失效段落及下游；视觉证据或纠错
规则变化只失效 corrected 及下游；CSS 只失效 Web；Markdown 模板只失效导出，二者都不参与 ASR
缓存键。

## 输出

Obsidian 输出只写到用户显式提供的目录，并在覆盖前检查生成标记与上次 artifact 哈希。M4A
只导出 Markdown、证据 JSONL 和帧副本，不复制视频、模型或 Canvas。Web MVP 读取任务 JSON，
使用原生 HTML/CSS/JS 做五分钟客户端搜索，不依赖 CDN，媒体以 HTTP Range 提供且服务固定监听
`127.0.0.1`；所有字幕以 `textContent` 插入，资源路由限制在当前任务目录。

## M4B 技术内容压力测试

M4B 在既有层次内增加受限的 25 分钟编排器，而不是平行流水线。`m4b/service.py` 从不可变 ASR
时间轴生成唯一覆盖的技术段落，并按五个 300 秒 primary range 切包；相邻包最多读取前后 30 秒
context，`primary_transcript_ids` 决定唯一交付归属。每个 Packet 保存输入/输出哈希、实际图片
列表和校验结果，失败只使该包与合并下游失效。

帧去重仍由 Pillow 确定性完成，但公式/定义提示、局部像素变化、边缘变化、高对比文字变化、
时间间隔和高 scene score 可保护渐进式幻灯片。当前 Codex 串行写入每包 analysis；跨包合并只
消费已验证 JSON、段落索引、术语表、公式清单与边界报告，不重新读取原图。公式必须绑定
`frame_id`，Web/Obsidian 以可复制 LaTeX 和本地截图显示公式并隐藏 overlap。

媒体缓存身份使用稳定的 source/BVID/page/range/selector/tool 字段，不让无关下游配置或元数据
刷新触发重新下载。阶段中断后若 immutable artifact 仍通过输入哈希与 SHA 校验，可追加
`cache-restore` 成功 attempt 恢复，旧中断记录仍保留。
