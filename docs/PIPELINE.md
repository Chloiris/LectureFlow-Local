# Pipeline

## 阶段命令

```text
doctor
prepare → subtitles fetch/import → transcript_ready → media acquire → frames extract → build-packets
        → [Codex 交互分析] → validate → render
        → export-obsidian / serve
```

每个阶段在持有任务锁时计算输入哈希。成功且输入、输出哈希一致就跳过；失败会写入可恢复的
尝试记录。`resume` 从最近成功阶段继续，不删除已有产物。

Bilibili URL 未指定 `p` 时，来源身份表示完整多 P 集合；显式 `?p=N` 才表示单个分 P。
两者使用不同 job/cache identity，短链接规范化也必须保留显式 `p`。

## 字幕选择

严格优先：UP 主人工字幕 → Bilibili AI 字幕 → 用户本地字幕 → MLX-Whisper →
Faster-Whisper。选择到可靠来源后停止，不做重复转写。每个来源保存语言、生成时间、模型和
原始文件引用。

M1 完整实现前三项。公开 B站路径用 `/x/web-interface/view` 获取分 P、用当前 yt-dlp 同款
`/x/player/wbi/v2` 枚举字幕；响应和字幕字节本地缓存。单个本地后备字幕只允许绑定显式单 P，
避免多 P 串台。无公开字幕是可恢复结果，不是未捕获异常；系统也不会因此自动安装或下载 ASR。

```bash
lectureflow subtitles fetch "<B站URL>"
lectureflow subtitles import "<字幕文件>"
lectureflow subtitles inspect <job-id>
lectureflow subtitles export <job-id>
lectureflow resume <job-id>
lectureflow run "<source>" --until transcript_ready
```

`--force-stage subtitle_ready` 只失效字幕及下游阶段。相同成功任务先验证结果、派生文件和原始
文件哈希，再直接命中任务缓存，不进行 HTTP 请求。请求失败有界重试；状态保存脱敏失败原因。

`subtitles fetch --offline` 先验证任务元数据与字幕结果缓存。缓存完整时不构造任何网络请求；
结果尚未汇总但原始 API 响应齐全时可离线重建。任一必需缓存缺失都会明确失败，禁止偷偷联网。
无候选结果按分 P记录 A–F 分类：接口明确要求登录为 E，接口明确无候选且不要求登录为 D。

## M2 媒体与帧

```bash
lectureflow media acquire <job-id>
lectureflow media inspect <job-id>
lectureflow frames extract <job-id>
lectureflow frames inspect <job-id>
lectureflow frames contact-sheet <job-id>
lectureflow run "<视频来源>" --start 0 --end 300 --until frames_ready
lectureflow resume <job-id>
```

本地媒体不复制；B站媒体保存到任务目录并限制 1080p。受控依赖通过当前项目解释器的
`python -m yt_dlp` 调用，不依赖全局 shell。下载前从 yt-dlp 元数据按请求范围和受限
格式估算体积，超过 500 MiB 明确失败。根多 P URL 的媒体阶段默认选择 P1；显式 `?p=N` 处理
指定 P，选择理由写入清单。媒体缓存键与帧配置解耦，因此阈值变化不会重复下载。

帧候选按以下独立来源合并：

1. FFmpeg `scene` 检测并保存场景分数。
2. `frames.subtitle_cues` 匹配 timed transcript，并按 `cue_offsets_seconds` 前后补抓。
3. `periodic_interval_seconds` 周期安全采样，包含范围起点和必要的尾部采样。

FFmpeg 另检测黑区间并抽取中点用于质量审计。所有候选用 Pillow 验证 PNG，计算 SHA-256、
dHash、锐度、黑像素比例、平均亮度和亮度标准差。去重在全局候选间比较，优先保留提示词帧、
高场景分数和较清晰帧，再合并来源/提示词/segment ID。黑/极暗帧不进入最终集合，低信息帧
保留标记。默认最多抽 240 个候选、保留 30 个最终帧；每张联系表最多 12 帧且按时间排序。

覆盖报告显式列出每个分钟桶是否有最终证据帧、黑/极暗/低信息计数以及提示词 segment 覆盖比例。
缓存命中必须同时满足输入/配置哈希和 artifact manifest 的所有文件哈希。单张最终帧丢失或
损坏时按记录时间局部重抓并重建联系表；中断中的 `frames_ready` 尝试标记为可恢复后追加新尝试。
`--force-stage frames_ready` 只失效帧及其后代。

## 帧与 Packet 边界

候选帧结合场景变化、配置化字幕提示词与周期安全采样，经黑屏过滤和感知去重。每个联系表
仅用于筛选；公式、代码与小字必须回到高清原图。Packet 默认 420 秒并有 30 秒上下文重叠。
M2 不读取图片做语义理解；联系表筛选和高清原图理解从 M3 才开始。

## M3A 当前 Codex 视觉能力烟雾测试

M3A 是 M2 与完整 M3 之间的一次小型、人工编排能力验证，不是新的流水线阶段，也不改变
`frames_ready` 状态。它只复用已经通过 artifact 哈希检查的联系表、最终帧和候选对照：当前 Codex
先打开联系表定位顺序，再用本地 `view_image` 逐张打开所有最终高清原图。每次打开立即追加一条
`analyses/vision-smoke/visual-observations.jsonl`，视觉结论必须绑定 frame ID、时间点、相对路径和
SHA-256。

`analysis-provenance.json` 是实际打开列表的权威声明；校验器要求它与 observation 路径集合完全
一致，并检查 frame ID 存在、清单路径与哈希一致、文件实际哈希一致、全部最终帧已打开、
`image_opened=true`、外部模型/OCR/ASR 未使用，以及低信息和 dHash 重复对照记录存在。联系表观察
只能用于导航，不能作为最终高清分析。必要裁剪必须保留父 frame ID 和坐标；本次真实 M3A 没有
需要裁剪。

指定真实任务没有 M2 标记的黑屏、极暗或低信息候选，因此 M3A 只记录一个不冒充真负样本的
最低亮度替代对照，并把验收结论降为部分通过。dHash 重复对照经逐图复核合理。M3A 不生成
Packet、证据账本、笔记、Obsidian、Mermaid、Canvas 或 Web。

## Codex 边界

第一阶段由当前会话读取本地 Packet 并写分析 JSON。`codex exec` provider 即使检测可用也
默认关闭；启用前必须再次读取当前版本帮助、估算 Packet 数并由用户显式开启。

## M3B 本地 ASR 与单 Packet

```bash
lectureflow asr doctor
lectureflow asr preflight <job-id> --seconds 30
lectureflow asr transcribe <job-id> --backend mlx-whisper --start 0 --end 300 --offline
lectureflow asr inspect <job-id>
lectureflow packet build <job-id> --start 0 --end 300
lectureflow packet validate <job-id> P0001
lectureflow validate <job-id>
lectureflow evidence validate <job-id>
```

固定 MLX 模型已缓存时，严格 offline 路径完全不联网。第一次从现有媒体生成 16 kHz 单声道 WAV
并转写；第二次必须同时命中音频、模型和转写缓存。模型名/revision 或音频 SHA 变化会使 ASR
失效；笔记模板或抽帧配置不参与 ASR 键。原始 MLX 输出先落盘，再派生统一时间轴。

可选 VibeASR BitNet 路径必须显式指定，且运行前模型和官方二进制已经由用户在项目外准备：

```bash
lectureflow asr doctor --backend vibeasr-bitnet
lectureflow asr preflight <job-id> --backend vibeasr-bitnet --seconds 30
lectureflow asr transcribe <job-id> --backend vibeasr-bitnet --start 0 --end 300 --offline
```

它使用独立 24 kHz 音频和官方 `text` prompt，保留每个纯文本 chunk 的原始输出；start/end 只表示
最多 30 秒的确定性音频块，不是模型对齐时间，speaker 保持 null。热词只作为识别上下文，不能
作为术语正确性的独立证据。默认 `run` 和 `auto` 仍选择 MLX；已有 transcript 的 job 不得
切换后端，比较需使用新 workspace。运行时显式传递 context size，并对输入预算、decode/prefill
错误和输出 token 上限执行 fail-closed 门禁。LectureFlow 不提供自动模型下载、仓库克隆或编译命令。

M3B 只构建一个 P0001。当前 Codex 读联系表、时间戳字幕、M3A 观察与入选高清原图，生成分析
JSON；本地校验器检查 Schema、图片 provenance、transcript/frame ID 后，才允许生成并验证证据
账本和五分钟笔记。没有使用 `codex exec` 或子 Agent。

## M4A 段落级本地阅读 MVP

```bash
lectureflow paragraphs build <job-id>
lectureflow corrections build <job-id>
lectureflow render <job-id>
lectureflow export-obsidian <job-id> --vault ".tmp/m4a-obsidian-smoke"
lectureflow serve <job-id> --host 127.0.0.1 --port 8765
```

M4A 只复用现有 17 个 ASR segment，不请求 word timestamps、不重转写。Codex 分组计划只能引用
相邻 segment；本地校验器负责 start/end、唯一覆盖、raw 哈希、Schema 与缓存。纠错建议先写
决策账本，只有高置信 transcript+frame 证据项可自动 accepted；pending 不应用。

章节和导图只覆盖 P1 前五分钟。阅读器以段落开头为所有跳转粒度，原生前端支持三层文字、
搜索、播放高亮、章节/树/证据/帧跳转；服务拒绝非 loopback host 和路径穿越，并用 HTTP Range
提供现有媒体。Obsidian 导出使用受控 `LectureFlow Demo` 目录、相对链接和 B站段首时间链接；
若生成物被手工修改则拒绝覆盖。该阶段不生成 Canvas，也不是完整课程处理。

## M4B P6 25 分钟压力测试

```bash
lectureflow run "https://www.bilibili.com/video/BV1pf421z757?p=6" \
  --start 0 --end 1500 --until transcript_ready
lectureflow frames extract <job-id> --config config/m4b-p6.toml
lectureflow paragraphs build <job-id>
lectureflow packets build <job-id>
lectureflow packets validate <job-id>
lectureflow analyze-packets <job-id>
lectureflow merge <job-id>
lectureflow evidence validate <job-id>
lectureflow render <job-id>
```

真实 P6 使用五个 primary range：`0–300`、`300–600`、`600–900`、`900–1200`、`1200–1500`；
Packet 读取范围向相邻包最多扩 30 秒。overlap 只作 context，只有 primary ID 可以进入最终正文。
每包先校验 Schema、ID、图片哈希、打开列表和 API=false，再串行进入下一包。

公式提示触发 -2/0/+2/+5 秒候选。dHash 近似帧还需经过局部像素、边缘、高对比文字、时间间隔、
scene score 与 cue 保护。`merge` 去重段落、transcript、术语和公式，生成边界报告及统一输出；
Web/Obsidian 隐藏 overlap。CSS、Markdown 或公式展示变化不参与媒体、音频、ASR 或抽帧缓存键。

## M5 完整 P6 增量流程

```bash
lectureflow p6 plan "https://www.bilibili.com/video/BV1pf421z757?p=6"
lectureflow freeze <m4c-job> --range 0:1500
lectureflow run "https://www.bilibili.com/video/BV1pf421z757?p=6" --start 1500 --end 3300.33
lectureflow p6 materialize-transcript <incremental-job> --frozen-job <m4c-job>
lectureflow frames canonicalize <incremental-job> --frozen-job <m4c-job>
lectureflow paragraphs build <incremental-job>
lectureflow packets build <incremental-job>
lectureflow analyze-packets <incremental-job>
lectureflow p6 merge --frozen-job <m4c-job> --incremental-job <incremental-job>
```

重跑时验证音频/模型/转写、绝对时间 transcript、段落输出哈希、Packet 输入输出哈希和已完成
Packet status。分析阶段不得改写段落构建输入；未登记的中断目录移出 active Packet namespace，
保留于可恢复的 orphan 目录。
