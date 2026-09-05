# Changelog

所有重要变更记录于此。版本遵循语义化版本。

## [Unreleased]

暂无。

## [0.3.0] - 2026-09-05

### Added

- 完成首个公开源码快照的发布加固：排除真实任务工作区，增加离线 CI、安全报告、贡献说明、
  第三方边界和明确的专有许可声明。

- 新增显式 `vibeasr-bitnet` 本地 ASR 选项：适配微软官方 VibeASR.cpp 1.5B BitNet 的纯文本与
  hotword 输出，使用独立 24 kHz 音频、确定性 30 秒粗粒度时间块及完整运行 provenance。
- 新增 VibeASR doctor、preflight、CLI、固定 revision/双 GGUF 大小及 SHA-256 门禁；默认关闭且
  不自动克隆、编译、下载或回退云端。

- 完成 M5：冻结 P6 前 25 分钟的 36 项 artifact 哈希，增量处理 25:00–55:00.33，并在 `merged/p6-complete` 交付完整 P6，不重编号或重算冻结内容。
- 添加绝对时间增量 transcript、稳定追加 ID、6 个串行 Packet、非破坏式 frame canonicalization、25:00 边界审计、完整证据/笔记/导图和两段媒体阅读器。
- 新增完整 P6 临时 Obsidian 导出与 1440×900 浅/深色浏览器验收；媒体切换、段落高亮、术语/公式搜索和长公式滚动通过。

- 完成 M4C P6 内容质量审计：8/8 公式盲读复核、32/32 accepted 纠错复核、247/247 术语候选
  对账、15 段保真抽查、4 个 Packet 边界核验和 16 组候选帧去重对照。
- 添加版本化 M4C 公式/纠错/段落/修复 Schema、审计 CLI、证据完整性、可恢复缓存、结构化报告与
  人工复核队列；公式 crop 坐标和 Attention 关键结构具备门禁。
- 为本地阅读器的长公式增加不换行水平滚动，并提供仅本地 query 参数控制的浅/深色审计入口；
  1440×900 内置浏览器实测 8/8 公式完整、高清弹窗可开、无第三方资源。

### Fixed

- 使用当前 FFmpeg 支持的 `-fps_mode vfr` 抽取场景帧，并将公开 CI 约束到项目实际支持的 macOS
  目标平台，避免不同 FFmpeg 构建产生不可比的候选帧集合。

### Changed

- ASR backend identity 现在进入 transcript 缓存和状态审计；已有不可变 transcript 的 job 拒绝
  切换后端，避免比较模型时覆盖 raw。

- ASR 音频缓存可在先前可恢复失败后注册已验证缓存成功尝试，且不回退当前里程碑。
- 增量 Packet 分析把纠错段落另存为派生物，不再改写 paragraph 构建输入；二次运行同时命中 paragraph、Packet 和分析缓存。
- 未登记的中断 Packet 会被原子归档到 `packets/incremental-orphans/`，不再混入 active namespace；
  完整证据视图补充 frozen/incremental 来源、canonical frame 以及公式自身的 frame 引用。

- 将缺乏确切视觉证据的 `重密模型 → 稠密模型` 从 accepted 降为 pending，且不再应用到 corrected；
  将幻灯片标题纠正为画面真实复数 `Sparse Transformers`。原始 ASR 与 Packet 分析保持不变。
- M4B 合并现在在输入/输出哈希未变时保留成功 Packet 的分析状态时间戳，避免仅重建审计下游时
  改写上游审计记录。

- 初始化 uv/Python 3.12 项目、Git 忽略规则、架构/Schema/隐私/流水线文档。
- 添加只读 `doctor`、幂等 `prepare`、可审计 `status` 和 `resume` CLI。
- 添加 Bilibili 与本地来源识别、FFprobe 元数据、本地文件指纹和时间范围校验。
- 添加完整多 P 与显式分 P 的不同任务身份，并让工具版本参与元数据缓存键。
- 添加原子 JSON 状态机、不可变 job identity、JSONL 事件日志、任务锁和哈希缓存。
- 添加凭据脱敏、路径逃逸/符号链接防护和无 shell subprocess 封装。
- 添加项目级 LectureFlow Codex Skill 及 87 项离线单元/smoke 测试。
- 完成 M1：公开 B站分 P与字幕候选枚举，严格执行人工 → AI → 显式本地字幕优先级。
- 添加可测试的无 Cookie HTTP 客户端、有界重试、响应缓存、可信下载域与凭据/签名脱敏。
- 添加 B站 JSON、SRT、VTT、untimed TXT 解析，版本化统一时间轴和任务内唯一 segment ID。
- 添加内容寻址的不可覆盖原始字幕，以及 original/cleaned JSONL、TXT、SRT、VTT 全格式导出。
- 添加来源、选择、质量和 artifact manifest 报告，覆盖重叠、重复、乱码、稀疏与覆盖率审计。
- 添加字幕阶段缓存完整性验证、最小强制重跑、失败恢复和多 P 独立目录。
- 添加 `subtitles fetch/import/inspect/export`、M1 `resume`/`transcribe` 和一键 `run` CLI。
- 添加本地 ASR 抽象、能力检测和禁止自动模型下载的明确错误；未安装或下载 ASR 模型。
- 将离线单元/smoke 测试扩展至 117 项。
- M1.5 真实验收识别 `BV1pf421z757` 的 15 P 为 E（需登录）、P6 为 D（无字幕），未读取 Cookie。
- 修复无公开字幕结果被笼统归类且无法命中任务缓存的问题，新增逐 P A–F 分类字段。
- 新增严格 `subtitles fetch --offline`：只读已验证元数据/API/结果缓存，缺失时禁止网络回退。
- M1.5 测试增至 119 项，并记录真实选择/结果/HTTP 缓存哈希稳定性。
- 完成 M2：本地媒体只读复用、B站 ≤1080p 分段下载、500 MiB 预估门禁、FFprobe/完整哈希清单。
- 添加多 P 媒体选择审计、场景变化/字幕提示/周期采样、黑区间探针、Pillow 图片验证与质量指标。
- 添加全局 dHash 去重、质量优先保留、候选/最终预算、相对路径帧账本和每页最多 12 帧联系表。
- 添加帧候选、去重、质量、提示词、覆盖率和 artifact manifest 正式报告。
- 添加 `media acquire/inspect`、`frames extract/inspect/contact-sheet`、
  `run --until frames_ready`、状态/恢复、配置最小失效和单帧局部修复。
- 添加可重复生成的离线 M2 视频夹具及场景/公式/代码/黑屏/重复画面测试；总测试增至 135 项，
  覆盖率门禁不低于 86%。
- 完成指定真实 B站课程 P1 的 00:00–05:00 M2 smoke；实际媒体 32.5 MiB，19 候选、7 最终帧，
  缓存和产物哈希复验通过，未做视觉语义分析。
- 完成 M3A 当前 Codex 本地图像理解 smoke：用 `view_image` 打开联系表、全部 7 张高清最终帧、
  一个透明注明的低亮度替代对照和一组真实 dHash 重复对照。
- 添加版本化 visual observation Schema、分析 provenance 及 frame ID/路径/SHA/打开列表/安全
  标记/负面对照完整性校验和定向测试。
- 真实 M3A 能可靠读取 `Course Info` 与 `Teaching Team` 等幻灯片内容；未发现公式或代码，
  dHash 去重视觉复核通过。因 M2 没有任何黑屏/低信息标记候选，最终结论明确为部分通过。
- 完成 M3B 本地 ASR：uv Python 3.12.13 上运行 MLX 0.32.0/MLX-Whisper 0.4.3，仅使用固定
  `mlx-community/whisper-medium-mlx` revision，生成音频清单、原始输出、统一时间轴与质量报告。
- 真实 P1 前五分钟转写 17 段、耗时 29.091 秒、RTF 0.096971；第二次命中音频/模型/转写缓存，
  原始输出哈希稳定。30 秒预检和 13 点人工质量抽查均已记录。
- 添加单一 P0001 Packet、分析/证据 Schema、Packet 哈希、ID/时间/provenance/API 安全门禁；
  当前 Codex 实际读取联系表和 4 张高清帧，生成 9 条证据及五分钟综合学习笔记。
- 添加本地 ASR/Packet/Evidence 定向测试和完全离线的合成五分钟 M3B smoke；未实现完整课程、
  Obsidian、Canvas、Mermaid 或 Web。
- 完成 M4A 五分钟段落级 MVP：17 个原始 ASR segment 全量唯一合并为 8 个可读段落，段首/段尾
  严格复用来源时间，raw / cleaned / corrected 分层且原始哈希不变。
- 添加可审计纠错决策、章节/重点/课程信息、JSON/Markdown/Mermaid 层级导图；高置信
  `大元模型 → 大语言模型` 绑定 T000007/F000002，pending 项不进入正文。
- 添加仅绑定 `127.0.0.1` 的原生三栏阅读器、HTTP Range 视频 seek、段落高亮、搜索、章节/导图/
  证据/关键帧跳转和路径/XSS 防护，不使用 CDN。
- 添加受控临时 Obsidian Markdown 导出、相对帧链接、B站段首链接、手工修改保护和幂等缓存；
  不生成 Canvas，不复制媒体或模型。
- 完成 M4B P6 00:00–25:00 技术课程压力测试：复用固定 medium MLX 模型完成 777 段本地转写，
  全量合并为 24 段，并构建 5 个带 30 秒上下文 overlap 的可审计 Packet。
- 增加公式提示帧保护、局部/边缘变化检测、渐进式 PPT 报告和每 Packet/全局帧预算；真实任务
  196 候选保留 98 最终帧，实际视觉对照未发现新增教学内容被 dHash 误删。
- 增加 M4B Packet/公式/推导/术语/纠错/跨包合并 Schema 与门禁；生成 8 条绑定 frame ID 的
  公式、16 项术语表、44 条纠错决策和无重复的统一章节/证据/笔记。
- 扩展三栏阅读器和临时 Obsidian 导出，支持长段落、公式/术语检索、公式本地截图、待核验标记、
  25 分钟 Range seek 与 5 Packet 合并结果；无 CDN、Canvas、媒体或模型复制。
- 修复 B站范围媒体缓存使用易变元数据哈希、阶段中断后无法恢复有效 artifact 的问题；缓存恢复
  追加独立成功 attempt，仍验证 source/page/range/tool/content SHA。
