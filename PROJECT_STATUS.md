# Project Status

更新时间：2026-09-05

## 当前阶段

M5 — 已冻结 M4C 审计范围，并以增量方式交付完整 P6；不代表其他分 P 或整套课程完成。

## 已完成

- 已完成公开源码快照加固：真实任务工作区不再跟踪，发布历史使用 GitHub noreply 身份，并补充
  离线 CI、安全报告、贡献边界、第三方说明和专有许可声明。
- 已冻结 P6 `00:00–25:00` 的 36 项上游 artifact 哈希和全部既有 ID；结束复核无变化，未重新下载、转写、抽帧或分析冻结范围。
- 已检测 P6 实际时长 3300.33 秒，仅下载并处理 `1500–3300.33` 秒：252,452,524 字节、1920×1080；播放器使用两段媒体和统一绝对时间轴。
- 已复用固定 medium MLX 模型完成新增 926 段、10,101 字本地转写，耗时 216.77 秒、RTF 0.120406；二次运行音频、模型、transcript 均命中缓存。
- 已生成新增 27 段、6 个串行 Packet、25 张稳定追加 ID 帧（24 active），当前 Codex 打开 17 张高清新增帧；7 个新增公式经独立视觉审计为 6 high / 1 medium。
- 已在合并层生成完整 P6：1703 segment、51 段、14 章、28 个术语、15 个公式和 51 条证据；证据悬空与 25:00 正文重复均为 0，教学主体覆盖率 99.70%。
- 已实测完整 P6 阅读器的 Range、25:00 切换、段落高亮、术语/公式搜索、长公式滚动与 1440×900 浅/深色页面；临时 Obsidian 导出幂等，应用内视觉仍待人工验收。
- 已增加显式 `vibeasr-bitnet` 可选后端：固定微软官方 revision、二进制哈希和两份 GGUF 的
  大小/SHA，支持纯文本与 hotwords，使用独立 24 kHz 音频及确定性 30 秒粗粒度时间块；默认关闭，
  不会自动下载或编译。官方 1.5B 不提供模型时间戳或 speaker，系统没有伪造这些字段。

- 已确认 Apple M5/arm64、FFmpeg/FFprobe、SQLite FTS5、uv 与 Python 3.12 可用。
- 已确认系统裸 `python3` 为 3.9，项目固定通过 uv 使用 Python 3.12。
- 已在 uv 隔离环境安装并锁定 yt-dlp；M3B 后续仅安装 MLX-Whisper 和一个固定 medium 模型，
  Faster-Whisper 与其模型仍未安装。
- 已确认本机 Codex CLI 具有文本、图片和 JSON Schema 参数；provider 仍默认关闭。
- 已建立 uv/Python 3.12 隔离环境、Git 仓库、配置与隐私边界。
- 已实现 `doctor`、`prepare`、`status`、`resume` 及全部规划命令的帮助入口。
- 已实现 Bilibili BV/av/多 P URL 解析、本地文件指纹和安全 job ID。
- 已按 yt-dlp 官方 `2026.07.04` 语义区分完整多 P 与显式 `p=N`，并保留短链接页码。
- 已实现原子 manifest/state、不可变 job identity、追加事件账本、任务锁与阶段尝试历史。
- 已实现真实 FFprobe 元数据、输入/配置/产物哈希缓存、`--force-stage metadata_ready` 和中断恢复。
- 已实现路径逃逸/符号链接防护、subprocess argv 隔离和 Cookie/token/签名 URL 脱敏。
- 已创建并通过校验的项目级 `lectureflow-local` Codex Skill。
- 已实现公开 B站分 P/字幕候选枚举、人工优先于 AI、单 P 本地字幕后备及无字幕结构化结果。
- 已实现无 Cookie 的 HTTPS 客户端、有界重试、原始响应缓存、可信字幕下载域和全链路脱敏。
- 已实现 B站 JSON、SRT、VTT、untimed TXT 解析与任务内唯一 segment ID；多 P 不串台。
- 已实现不可覆盖的内容寻址原始字幕、原始/清理 JSONL、TXT/SRT/VTT、来源/选择/质量报告。
- 已实现字幕产物/原始材料哈希校验、幂等缓存、`--force-stage subtitle_ready` 和导出失败恢复。
- 已实现 `ASRBackend`/`TranscriptResult`、MLX 优先能力检测和“本地模型未配置”安全错误。
- 已实现 `subtitles fetch/import/inspect/export`、`transcribe`、M1 `resume` 和
  `run --until transcript_ready`。
- 已对 `BV1pf421z757` 完成真实无 Cookie 验收：15 P 分类 E（需登录），P6 分类 D（无字幕）。
- 已增加无字幕成功结果缓存及严格 `subtitles fetch --offline`；缓存缺失时绝不联网。
- 已实现 `media acquire/inspect`：本地媒体只读复用，B站按范围下载到任务目录，完整 SHA-256
  校验，估算超过 500 MiB 时拒绝并要求确认。
- 已明确多 P 媒体策略：显式 `?p=N` 处理该 P；根多 P URL 在 M2 默认选 P1，并把选择理由
  写入 `media-manifest.json`。
- 已实现 FFmpeg 场景变化、配置化字幕提示词前后偏移、周期安全采样和黑区间质量探针。
- 已实现候选帧完整哈希、64 位 dHash、全局非相邻去重、黑/极暗过滤、低信息标记、质量优先
  保留和最大 30 张最终帧预算。
- 已实现 `frames.jsonl`、候选清单、去重/质量/提示词/覆盖率报告、每页不超过 12 帧的本地
  联系表和 artifact manifest。
- 已实现 `frames extract/inspect/contact-sheet`、兼容 `extract-frames`、
  `run --until frames_ready`、状态展示、缓存恢复、配置最小失效和单帧局部修复。
- 已对指定真实视频 P1 的 00:00–05:00 完成无 Cookie M2 smoke：媒体 32.5 MiB、19 候选、
  12 个重复候选被审计丢弃、7 张最终帧、1 张联系表。
- 已复用同一 `frames_ready` 任务完成 M3A：当前 Codex 用 `view_image` 实际打开 1 张联系表、
  全部 7 张原始高清最终帧和 3 张候选对照，并逐项保存 frame ID、时间、路径和 SHA-256。
- 已确认这 7 张帧包含课程封面、`Introduction to Large Language Models`、`Course Info` 和
  `Teaching Team`；未发现可可靠转写的公式、代码、图表或架构图，没有为通过测试而补写。
- 已视觉复核 dHash 候选 `C000012`/`C000014`：教学主体相同，去重合理，未发现误删。
- 已增加 M3A observation JSON Schema、图像/哈希/frame ID/打开列表/provenance/负面对照门禁。
- 已在 uv Python 3.12.13 环境接入 `mlx 0.32.0`/`mlx-whisper 0.4.3`，Metal 实机可用；只下载
  `mlx-community/whisper-medium-mlx@7fc08c4...` 一个模型（1,524,927,044 字节）。
- 已从缓存媒体生成 300 秒、16 kHz、单声道 PCM WAV；30 秒预检和真实五分钟本地转写通过，
  五分钟耗时 29.091 秒、RTF 0.096971，生成 17 段及 JSONL/TXT/SRT/VTT。
- 已构建和校验唯一 `P0001` Packet：包含 17 个 transcript ID、7 帧 manifest、M3A 观察、
  联系表、视频/音频清单、固定分析 Schema 与 artifact 哈希。
- 当前 Codex 本轮实际打开联系表和 `F000001`、`F000002`、`F000004`、`F000007` 高清原图，
  生成 5 个小节、10 条分析 finding、9 条证据账本记录和五分钟综合学习笔记。
- ASR 原始 JSON 与 original.jsonl 不可变；`Chet GPT`、`大元模型`、`萧朝君`等只形成纠错建议，
  没有覆盖原始转写。第二次转写命中音频、模型、转写三层缓存且原始哈希稳定。
- 已在不重跑 ASR 的前提下，把 17 个原始 segment 全量唯一合并为 8 个段落；平均 37.375 秒、
  最长 65 秒，所有 start/end 直接来自来源 segment，raw SHA-256 保持不变，未生成词级时间戳。
- 已生成 raw / cleaned / corrected 三层文本和逐段 cleaning diff；三条纠错中 1 accepted、2 pending。
  `大元模型 → 大语言模型` 由 T000007/F000002 支持并应用，pending 项未进入正文。
- 已生成 5 个五分钟章节、9 条重点、课程信息和可跳转的 JSON/Markdown/Mermaid 层级导图；
  内容明确限制为课程介绍，不冒充完整知识讲义。
- 已实现只绑定 `127.0.0.1` 的原生三栏阅读器：现有视频 Range/seek、段落跳转与播放高亮、三层
  文字、搜索、章节/导图/证据/高清帧跳转，无 CDN、无第三方请求并阻止路径穿越。
- 已用 Codex 内置浏览器在 1440×900 真实检查页面：视频可加载、无横向溢出、段落可读、帧弹窗
  可用；浅色页面实际截图通过，深色由本地 `prefers-color-scheme` CSS 提供且无远程资源。
- 已向 `.tmp/m4a-obsidian-smoke/LectureFlow Demo` 完成临时 Vault 导出及二次幂等复跑；相对图片、
  B站段首时间链接、三层 transcript、纠错脚注和 Mermaid 均通过检查，不含视频、模型或 Canvas。
- 已对 P6 00:00–25:00 建立独立任务并复用现有 medium MLX 模型：1500 秒本地转写生成 777 段、
  8,607 字，耗时 174.861 秒、RTF 0.116574；媒体、音频、模型与 transcript 复跑均命中缓存。
- 已将 777 个 segment 全量唯一合并为 24 段（平均 62.5 秒、最长 82 秒），按 5 个 300 秒
  primary range 与双侧最多 30 秒 context overlap 构建并校验 Packet；合并后无重复正文。
- 已提取 196 个候选、保留 98 张最终帧；公式提示覆盖 32/35，151 个渐进式变化因局部/边缘/
  提示词保护而保留。当前 Codex 串行分析 5 个联系表和 25 张高清帧，并另开 4 张真实渐进式
  对照，共实际查看 29 张高清图，未使用 OCR 或外部模型 API。
- 已生成 8 条可审计公式、1 条推导链、16 项术语表、44 条纠错决策（32 accepted、12 pending），
  公式均绑定 frame ID；不确定的口述系数与符号进入 uncertainties，未凭教材常识补全。
- 已生成 8 章、25 分钟证据账本、公式/术语/综合/复习笔记和导图；语音教学时间覆盖 100%，
  ±8 秒窗口视觉/联合覆盖 25.47%，公式提示画面覆盖 91.43%。
- 已扩展本地三栏阅读器与临时 Obsidian 导出，支持 24 段、8 章、术语/公式搜索、公式帧弹窗、
  Range seek 和纠错状态；1440×900 实机浏览器检查无横向溢出、无第三方请求。
- 已对全部 8 个公式完成先盲读原图、后比对既有 LaTeX 的独立审计；8 条均 exact match，关键
  符号编造和 critical error 为 0。Attention 原图明确显示完整括号、末尾 V 与分母
  `sqrt(d_k)`；1396–1404 秒严重失真的口述未用于补写卷积公式。
- 已逐项审计 M4B 的 32 个 accepted 纠错：31 项直接受证据支持，1 项因引用画面未显示确切词
  降为 pending，accepted precision 为 96.875%；另把 `Sparse Transformer` 修为画面所示复数
  `Sparse Transformers`，raw ASR 与 5 个 Packet 原始分析哈希保持不变。
- 已将 247 个术语候选完整对账为 215 个 normal term、31 个 accepted correction、1 个 pending，
  unreviewed=0；候选词与疑似错误在数据与页面中明确分离。
- 已抽查 15/24 段（每 Packet 至少 2 段）及全部 4 个既知跨 Packet 边界，关键遗漏、无证据扩写、
  原意改变、正文重复、遗漏与推导断裂均为 0。
- 已用 `view_image` 审查 16 组、47 张候选帧对照：8 组 safe duplicate、6 组 meaningful
  progression、2 组非教学小变化；渐进公式误删和关键标注损失均为 0。为保持既有引用稳定，
  本轮不改 98 个最终 frame ID，只将保守去重的 8 组 false negative 记录为后续策略证据。
- 已用 Codex 内置浏览器在 1440×900 检查浅/深色页面、8 个公式文本、Attention 高清弹窗及术语/
  公式搜索；长 LaTeX 可复制并水平滚动，截断 0，无第三方资源或控制台错误。
- 已对 `.tmp/m4c-p6-obsidian-audit` 做 Markdown 结构审计：8 个独立块级公式、16 个未缩进的
  `$$`、相对图片和 B站时间链接全部有效；未打开 Obsidian 应用，因此不声称应用内视觉渲染。

## 正在进行

- 无；M5 已停止在完整 P6 的增量交付，不进入其他分 P、整套课程、Canvas、RAG 或桌面应用。

## 已知问题

- 真实测试课程有 15 P 的字幕接口需要登录；按约束不请求 Cookie，P6 确认无字幕。
- 本地 MLX ASR、五分钟 M4A 与本轮受控 5-Packet 压力测试已实现；Faster-Whisper 未安装模型，
  通用课程级批处理、Canvas、RAG 和桌面应用仍未实现。
- 指定真实课程无登录态下没有公开字幕，因此真实 M2 提示词候选为 0；离线 fixture 已覆盖
  提示词命中及 segment ID 关联。没有据此下载 ASR 模型或伪造字幕。
- M2 真实任务的质量报告记录 `black=0`、`extreme_dark=0`、`low_information=0`，没有可供
  M3A 打开的已标记黑屏/低信息候选。最低亮度未保留候选 `C000002` 只作为透明注明的替代对照；
  因而 M3A 验收为部分通过，不能宣称黑屏负面对照已覆盖。
- 未请求或触碰用户真实 Obsidian Vault；M4A 只写被 Git 忽略的临时 smoke Vault。
- 五分钟 ASR 覆盖 269/300 秒；236–265 秒存在 29 秒无字幕区间，`T000015` 长句被压到
  235–236 秒，精确时间对齐需人工复核。没有参考真值，不能报告 WER。
- P6 已验证公式型课程的段落阅读效果，但 8 个公式均来自当前 25 分钟且没有代码样例；页面仍只
  保证段落级跳转，不提供逐句、逐词或卡拉 OK 高亮。视觉覆盖不是逐秒覆盖，低置信内容仍需人工核验。
- VibeASR BitNet 当前只完成代码适配和离线模拟测试；官方模型与 `VibeASR.cpp` 尚未在本机下载、
  编译或真实转写，不能宣称其精度、RTF 或 M5 兼容性已经通过 LectureFlow 验收。
- ASR manifest 的任一派生产物损坏目前会 fail-closed；系统不会覆盖 raw/original，但尚未实现只从
  immutable original 自动重建 cleaned/SRT/VTT/quality 的修复命令。

## 测试结果

- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：通过。
- `uv run pytest --cov=lectureflow --cov-fail-under=87.86`：276 passed，覆盖率 87.87%。
- VibeASR 定向测试覆盖配置/doctor、runtime/双 GGUF 完整性、text argv/context、确定性 chunk
  边界、静音块、截断/解码失败、预检裁剪、独立 24 kHz 缓存、backend lock 和 raw 不可变。
- M5 定向测试覆盖冻结 artifact、稳定追加 ID、绝对时间增量 transcript、可恢复 Packet、非破坏式
  frame canonicalization、25:00 边界、完整 P6 合并、两段媒体 Range/路径防护及临时 Vault 幂等。
- M4C 定向测试覆盖公式盲审顺序/high 门禁/crop 坐标/Attention 结构、32 项纠错完整审计、
  247 项术语对账、15 段/4 边界/16 去重组、证据与渲染门禁，以及 raw/candidate/Packet 状态不变。
- M4B 定向测试覆盖 25 分钟独立 job/range、稳定媒体与阶段恢复缓存、技术段落、5 Packet overlap/
  primary 唯一覆盖、公式/术语/纠错/证据引用、渐进式帧保护、长页面 Range/路径/XSS 和 Vault 幂等。
- M4A 新增 27 项定向测试，覆盖段落唯一覆盖/时间边界/raw 不可变、纠错门禁、章节/导图引用、
  Web Range/loopback/路径/XSS/跳转数据/三层文本，以及 Obsidian 相对链接/幂等/用户修改保护。
- 真实 CLI 验收：`uv sync --dev --locked`、doctor/status/paragraphs/corrections/render/export 通过；
  段落、章节/Web 二次构建及 Obsidian 二次导出命中缓存，未调用 ASR。
- M4B 真实 CLI 复跑：run/frames/paragraphs/packets/analyze/merge/evidence/render/export/resume 通过；
  audio/model/transcript、frames、paragraphs、Packet、merge、Web 和 Obsidian 均命中缓存。
- M3A 定向测试 17 项：合法观察、必填 frame ID、路径/高清/裁剪约束、Schema 写出、图片存在性、
  manifest 与实际 SHA、provenance 一致性、API/OCR/ASR 安全标记及负面对照门禁均通过。
- M3B 质量门禁：ASR/CLI/Packet/Evidence/状态恢复定向测试及真实 FFmpeg 音频提取的离线
  五分钟端到端 fixture 均通过。
- 真实离线媒体 smoke：FFmpeg 生成 2 秒音视频，中文/空格路径首次 prepare 成功，二次命中缓存。
- `lectureflow doctor --json`：核心环境通过；确认未联网、未检查 Cookie、未调用外部模型 API。
- Skill `quick_validate.py`：通过；独立只读前向测试遵守 Packet/视觉/恢复/导出边界。
- 最终独立门禁审查：未发现可复现的 P0/P1/P2。
- M1 离线 smoke：中文/空格路径完成创建、JSONL/TXT/SRT/VTT/质量报告、二次缓存命中、
  `inspect/export/status/resume/run`。
- B站 fixture smoke：人工/AI/无字幕/多 P/指定时间范围/本地后备、缓存复用和网络失败恢复通过。
- M1.5 真实 smoke：16 P/cid 正确；离线缓存重建成功；普通 fetch/resume 命中缓存；哈希稳定。
- M2 离线 smoke：FFmpeg 生成 32 秒含场景切换、公式/代码、黑屏与非相邻重复画面的
  真实视频；三路候选、去重、黑帧、预算、联系表、中文空格路径、缓存、配置失效、中断恢复和
  单帧修复全部通过。
- M2 真实五分钟 smoke：P1 300 秒 1080p 文件完整哈希通过；第二次媒体/抽帧/resume 均命中
  缓存，关键产物 SHA-256 不变；未读取任何关键帧做内容理解。
- M2 真实恢复 smoke：主动中断 `frames_ready`，旧尝试被标为 recoverable，`resume` 追加成功
  尝试；逐分钟证据表显示 5 个分钟桶中 4 个有最终帧，第 3 分钟的周期候选被全局重复合并。

## 下一步

- M5 到此结束，不自动进入其他分 P、整套课程、Canvas、RAG、桌面应用或批处理。
- 真实任务媒体、音频、模型、帧、截图和临时 Vault 均保持 Git 忽略；仓库只提交实现、测试和文档。
