# Data Schema

## 版本策略

每个权威 JSON/JSONL 文件包含 `schema_version`。兼容新增字段使用 minor 版本；删除、改名或
语义变化使用 major 版本并提供显式迁移。读取器拒绝未知 major 版本，绝不静默降级。

## Manifest

`manifest.json` 固定任务身份：job ID、来源类型、规范化来源、Bilibili BV/av 与分 P、本地
文件只读指纹、请求时间范围、创建时间、配置哈希和 pipeline 版本。

`job-identity.json` 是创建阶段的不可变来源与时间范围记录；`created.output_hash` 指向这个
文件。`manifest.json` 可随当前指纹、配置和已知产物路径更新，因此不冒充创建阶段的不可变
输出。

## Pipeline state

`pipeline-state.json` 保存整体状态、最后成功里程碑和每阶段尝试历史。每次尝试记录 UTC
起止时间、输入/输出/配置哈希、工具与版本、脱敏错误和可恢复性。

## Transcript JSONL（Schema 1.0.0）

```json
{"schema_version":"1.0.0","segment_id":"T000123","start":194.26,"end":216.81,"text_raw":"原始字幕","text_clean":"清理后的字幕","source":"bilibili_ai","language":"zh-CN","confidence":null,"speaker":null,"chapter_id":null,"part_id":"BV...-p2","source_ref":{"raw_file":"raw/subtitles/parts/...original.json","body_index":122}}
```

`start`/`end` 是有限、非负浮点秒，且 `end >= start`。`text_raw` 不可变；`text_clean` 只允许
确定性 Unicode/空白/标点清理。任务内 `segment_id` 唯一且在同一输入/配置下稳定；多 P 用
`part_id` 隔离，时间不跨 P 累加。清理版可删除相邻重复段，但原始 JSONL 永远保留全部段及 ID。

没有时间信息的 TXT 使用文档级结构：

```json
{"schema_version":"1.0.0","timed":false,"source":"local_file","language":"zh-CN","part_id":"local","segments":[],"untimed":{"schema_version":"1.0.0","kind":"untimed_transcript","text_raw":"原文","text_clean":"原文","source":"local_file","language":"zh-CN","part_id":"local","source_ref":{}}}
```

## M1 字幕产物

每个字幕/分 P 目录必须包含：

```text
original.json                 权威 TranscriptDocument
original.jsonl                全部原始段
cleaned.jsonl                 确定性清理及相邻去重结果
transcript.txt                可读时间范围
transcript.srt
transcript.vtt
subtitle-source.json          来源、语言、分 P/cid、发布时间、原始文件哈希
subtitle-selection-report.json 候选、选择原因与固定优先级
subtitle-quality-report.json  覆盖率与风险项
artifact-manifest.json        上述派生物 SHA-256
```

原始 B站响应和所选字幕字节保存在 `raw/subtitles/`。所选原始字幕文件使用内容哈希命名；清理或
重新导出不会写入该文件。`subtitle-result.json` 汇总任务状态、分 P 路径和最终来源。

真实无候选结果在 `subtitle-result.json` 保存 `classifications` 和 `classification_counts`。
分类代码遵循 M1.5 验收定义；D 表示接口明确无字幕，E 表示接口明确要求登录。两者不能合并为
未分类的“无字幕”。此时 `subtitle_ready` 可成功（枚举已完成），但 `transcript_ready` 保持
pending，且不会生成不存在的字幕产物。

质量报告含总时长、范围起点、并集覆盖时长/比例、字数、每分钟字数、空段、非法时间、重叠、
重复、超长、可疑乱码和长视频异常稀疏提示。报告只提示风险，不填补内容。

## M3B ASR 产物（Schema 1.0）

`transcript/asr/` 保存固定 MLX 模型的 `raw-mlx-whisper.json`、不可变 `original.jsonl`、确定性
`cleaned.jsonl`、TXT/SRT/VTT、runtime、质量报告和 manifest。manifest 记录音频 SHA、模型名、
revision、模型标称字节数、耗时、RTF、原始 JSONL 哈希和全部 artifact 哈希。模型缓存绝对路径
只存在受保护任务审计文件，不能提交 Git。

质量报告在通用字幕指标上增加 ASR 耗时、RTF、最大无字幕间隔、低文本密度、可能静音区间和
人工抽查。抽查仅使用 `understandable`、`obvious_error`、`suspected_error`、
`unable_to_judge`，不伪造 WER。

可选 VibeASR BitNet 使用同一统一 segment Schema，但 `source=vibeasr_bitnet`、`speaker=null`、
`confidence=null`，原始文件名为内容寻址的 `raw-vibeasr-bitnet-<sha-prefix>.json`。其 start/end
来自确定性音频 chunk，并在 source_ref 标记 `timestamp_basis=deterministic_audio_chunk_bounds`、
`model_timestamps_available=false`。runtime 记录 `asr_infer`、VAE/LM GGUF 的 SHA-256、线程数、
chunk/context/max tokens、热词列表和 `automatic_download_enabled=false`。MLX 与 VibeASR 缓存互不混用。

## M3B Packet 与分析（Schema 1.0）

`packets/P0001/packet.json` 固定 0–300 秒范围，列出 transcript/frame ID、媒体/音频清单哈希、
已知视觉词汇、证据约束、artifact 哈希和整体 packet hash。`P0001-analysis.json` 的每个 section
和 finding 至少引用一种真实证据；打开图片列表必须与 `m3b-provenance.json` 一致。纠错建议保存
原词、建议词、理由、证据和置信度，不修改时间轴。

## Media manifest（Schema 1.0.0）

`media/media-manifest.json` 记录来源类型、非敏感来源 ID、分 P ID、所选 P 与选择理由、请求范围、
任务相对媒体路径（本地外部源为 `null`）、存储方式、容器/时长/尺寸/编码、文件大小、完整
SHA-256、格式选择器、输入/配置哈希和工具版本。B站根多 P 默认选择 P1 时必须写：

```json
{"selected_part":1,"part_selection_reason":"default_first_part_for_media_stage","part_id":"BV..._p1"}
```

## Frame JSONL（Schema 1.0.0）

```json
{"schema_version":"1.0.0","frame_id":"F000042","candidate_id":"C000057","timestamp":194.26,"path":"frames/originals/F000042.png","reason":["scene_change","subtitle_cue"],"scene_score":0.47,"cue_terms":["这个公式"],"nearby_segment_ids":["T000121"],"width":1920,"height":1080,"sha256":"...","perceptual_hash":"0123456789abcdef","selected":true,"quality_flags":[]}
```

所有 `path` 都是任务相对路径。`frame-candidates.jsonl` 额外保存 `cue_offsets`、
`cue_offset_types`（before/at/after）、锐度、黑像素比例、平均亮度、亮度标准差和质量标记。
候选/最终图片均通过 Pillow 解码验证和 SHA-256 校验。

M2 正式审计物：

```text
media/media-manifest.json
frames/frame-candidates.jsonl
frames/frames.jsonl
frames/frame-dedup-report.json
frames/frame-quality-report.json
frames/cue-matches.json
frames/contact-sheet-manifest.json
frames/frame-artifact-manifest.json
reports/frame-coverage-report.json
```

去重报告逐项记录 `duplicate_drop`、`quality_drop` 或 `final_budget_drop`，以及保留候选和 dHash
距离。覆盖报告记录请求范围、各来源候选数、合并/抽取/去重/最终数量、来源分布、最大时间缺口、
逐分钟证据布尔表、每分钟帧数、预算丢弃、显式零值质量计数和提示词 segment 覆盖比例。联系表
manifest 每页最多包含配置允许的 12 帧。

## Evidence JSONL（Schema 1.0.0）

每条 claim 至少引用 `transcript_ids` 或 `frame_ids`，且带时间范围、证据类型、置信度和可选
不确定说明。没有证据引用的结论不能进入最终笔记。

M3B 的 `speech` 必须有 transcript ID，`slide` 必须有 frame ID，`speech+slide` 必须两者都有。
所有时间限制在 0–300 秒。`coverage.json` 按证据时间区间的并集计算语音、视觉与联合覆盖率，
不得用帧数量代替时间覆盖。

## M4A 段落、纠错与章节（Schema 1.0）

`transcript/paragraphs/paragraphs.jsonl` 的每条记录保存 `paragraph_id`、来源 segment 首尾时间、
raw / clean / corrected 三层文本，以及完整的 `source_segment_ids`、correction/evidence/frame ID。
段落必须由连续 segment 构成，`start` 等于首 segment start，`end` 等于末 segment end；同一主体
segment 必须且只能归属一次，时长不得超过 90 秒。`paragraph-build-report.json` 保存输入哈希和
raw ASR SHA-256，`paragraph-quality-report.json` 保存覆盖、重复、非法时间、时长和证据统计。

`transcript/corrections/correction-suggestions.jsonl` 与 `correction-decisions.jsonl` 分离建议和
决策。每条记录保留原词、建议词、段落/segment、时间、理由、transcript/frame 证据、置信度、
decision 与 applied。只有 `accepted + applied + high` 且同时有两类证据才可改变 corrected；
pending/rejected 只显示审计记录。

`analyses/m4a/sections.json` 的章节边界必须等于其首末 paragraph；摘要必须有 evidence ID。
`mindmap.json` 是权威树，节点只能指向已存在 section/paragraph 的开头；`mindmap.md` 和
`mindmap.mmd` 是确定性派生物。

## M4B 多 Packet 技术内容（Schema 1.0）

`TechnicalParagraph` 在三层文字基础上增加 `packet_ids`，最长 120 秒；最终主体段落中每个
segment 必须且只能出现一次。`TechnicalPacket` 保存 `time_range`、`primary_range`、
`overlap_range`、paragraph/transcript/frame ID、`primary_transcript_ids`、提示计数、输入哈希和
packet hash。overlap 只作 context，不能重复进入最终正文。

`FormulaRecord` 保存 formula/frame ID、时间、可选 crop、画面转录、LaTeX、口述上下文、引用、
变量、角色、置信度、不确定符号与 `verification_status`。低清公式允许空 LaTeX，但必须绑定原帧
并标 `needs_review`/`unreadable`。`PacketAnalysis` 还保存实际 `images_opened`、
`image_tool=view_image` 和强制 false 的外部 API 标记。

`TechnicalCorrection` 将 decision 与 applied 分开；pending 不改变 corrected。`TechnicalEvidence`
至少引用 transcript/frame/formula 之一；`speech+slide` 必须同时有 transcript/frame，
`speech+slide+formula` 还必须有 formula ID。附加审计物包括渐进式帧报告、每包校验、边界报告、
formula evidence 和 evidence quality report。

## M4C 内容质量审计（Schema 1.0）

`formula-blind-transcriptions.jsonl` 与 `formula-comparison-decisions.jsonl` 分别保存阶段 A 的独立
看图转写和时间更晚的阶段 B 比对；前者不能含既有 LaTeX。`formula-audit.jsonl` 汇合盲读时间、
既有 LaTeX 比对、关键符号可见性、置信度、
状态和可选 crop 坐标。`existing_latex_consulted_during_blind_pass` 只能为 false；high 公式要求全部
关键符号可见且无 uncertain symbol。Attention 项必须逐项检查参数、Softmax、转置、根号分母、
闭括号和末尾 V。

`correction-audit.jsonl` 为每个原 accepted 纠错保存独立支持判断与审计后 decision；只有
`supported + evidence_sufficient + meaning_preserved` 才能继续 accepted。`correction-overrides.jsonl`
是下游确定性重建的唯一覆盖输入，不能修改 raw ASR。

`term-candidate-reconciliation.json` 必须使 normal/correction/duplicate/noise/unreviewed 等状态之和
严格等于候选总数。其余审计文件覆盖段落样本、Packet 边界、候选帧对照、证据完整性、页面显示、
人工复核队列及逐项 before/after 修复记录；所有路径保持 job 相对路径。

## M5 冻结、追加与完整 P6

- `frozen/p6-00-25-artifact-hashes.json`：冻结 artifact 相对路径到 SHA-256 的分组映射。
- `metadata/p6-id-allocation.json`：各类 ID 的冻结最大值及 `max+1` 追加起点。
- `transcript/incremental/original.jsonl`：1500 秒后的绝对时间、稳定追加 segment ID。
- `frames/canonical/*.jsonl`：legacy/canonical ID、duplicate group 和 active 状态；不删除帧。
- `packets/incremental/Pxxxx/`：primary range 与最多 30 秒 context overlap。
- `merged/p6-complete/manifest.json`：冻结/增量 job、统一计数、媒体策略与冻结完整性。

完整 transcript 保留旧 ID 并追加新 ID；`corrected.jsonl` 只应用 accepted 纠错。
合并证据保存 `source_range` 和 `canonical_frame_ids`，但原 frame 引用不变；`needs_review`
公式保留 frame 并进入人工复核队列。
