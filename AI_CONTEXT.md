# AI Context

这些约束是项目的长期不变量：

- 不使用 OpenAI、DeepSeek、Gemini、Claude、Qwen、Groq 或任何其他外部模型 API。
- 目标平台是 Apple Silicon Mac；Python 运行时必须为 3.11 或更高版本。
- 视觉理解由当前 Codex 会话读取任务目录中的本地关键帧完成。
- 原始字幕永远不可覆盖；清理稿必须另存，并可回溯到原始 segment ID。
- 所有笔记结论必须绑定字幕 ID、frame ID 或明确时间点。
- Markdown 是展示格式，不是权威数据库；权威记录使用带版本的 JSON/JSONL。
- 不读取浏览器 Cookie 数据库或系统钥匙串，不在日志中暴露身份和 Cookie 字段。
- 不修改已有 BiliNote 或未被用户明确指定的 Obsidian Vault 路径。
- 模型预计下载超过 2 GB 时必须先向用户说明并取得确认。
- 本地 ASR 不得静默回退云端。已验收默认模型仍是固定 revision 的
  `mlx-community/whisper-medium-mlx`；可选 `vibeasr-bitnet` 只有在用户显式配置官方本地二进制与
  两份固定 GGUF 时启用。严格 offline 缺缓存必须失败，同一 job 不得覆盖切换模型。
- Codex CLI provider 默认关闭；交互 Packet 流程通过验收后才能显式启用。
- M2 的帧筛选仅使用 FFmpeg/Pillow 确定性指标；在进入 M3 前不得把图片送入任何模型、做 OCR、
  公式识别、代码理解或内容总结。
- M3A 仅验证当前 Codex 会话能直接读取本地关键帧：每张实际打开图片必须记录 frame ID、时间、
  任务相对路径、SHA-256、图片工具和打开时间；联系表不能替代原始高清图。
- M3A 的视觉结论只覆盖实际打开的 7 张唯一最终帧，不代表五分钟逐秒覆盖、完整课程理解或 M3
  已完成；没有清晰公式/代码时必须明确记录“未发现”，不得为能力验收编造内容。
- 当前 M3A 不使用 OCR、ASR、外部模型 API 或 `codex exec`；provenance 中任一对应标记为 true
  都必须使校验失败。
- M3B 原始 MLX JSON 与 `original.jsonl` 不可覆盖；`cleaned.jsonl` 只做确定性清理，专名或技术
  错词只能进入 correction suggestion。`speech+slide` 必须同时引用 transcript ID 和 frame ID。
- M3B 只覆盖 `BV1pf421z757` P1 前五分钟和 `P0001`；不应被描述为完整 M3、完整课程或最终笔记。
- M4A 只把同一五分钟任务做成段落级阅读 MVP：不启用词级时间戳，不重跑 ASR。段落边界只能
  取来源 segment 的起止时间，raw / cleaned / corrected 必须分层保存且 raw 哈希不可变。
- 自动接受纠错必须有高置信字幕与画面证据；pending 纠错不能进入 corrected 正文。M4A Web
  只绑定 `127.0.0.1`、不依赖 CDN，Obsidian 仅导出到显式临时/用户目录并拒绝覆盖手工修改。
- M4A 仅覆盖 P1 前五分钟的课程介绍，不代表完整课程，且尚未验证公式或代码型课程。
- M4B 仅覆盖 `BV1pf421z757` P6 的 0–1500 秒。它复用同一固定 medium 模型，保持段落级时间，
  通过 5 个串行 Packet 验证公式/术语/渐进式幻灯片；不能描述为完整 P6 或通用批处理完成。
- M4B Packet overlap 只提供上下文；每个 transcript、paragraph、formula 和正文 finding 只有一个
  primary 归属。合并阶段只能读取结构化 Packet 结果，不重新读取原图。
- 公式只有在高清帧足够清楚时才能写 LaTeX，并必须绑定 frame ID；不清楚时保留截图、空 LaTeX
  或 `needs_review`。pending 术语纠错不得应用到 corrected 正文。
- M4B Web 仍只绑定 loopback、无 CDN；公式以可复制 LaTeX 文本和本地截图呈现，不引入远程 MathJax。
- M4C 是对现有 P6 0–1500 秒产物的只读优先质量审计：公式必须先独立看图再读取既有 LaTeX；
  纠错只能按直接证据保留或降级。审计修复不得触发下载、ASR、候选帧提取或 Packet 重分析。
- M4C 的候选帧去重审计允许基于现有图片重新选择，但必须保持渐进公式和关键标注；若为引用稳定
  不改 frame ID，应把可安全压缩的候选明确记录为 `dedup_false_negative`，不能宣称算法已删除。
- Obsidian 临时导出只做 Markdown 结构检查时必须明确“未做应用内视觉审查”，不能等同于
  Obsidian 实际渲染验收。
- B站媒体预计超过 500 MiB 时必须先说明大小与用途并取得确认；抽帧配置变化不得触发重复下载。
- 所有媒体/帧 artifact 中的文件路径必须是任务相对路径；本地源绝对路径仅留在受保护 job
  manifest，不能进入帧清单、报告、Git 或最终文档。
- M5 冻结 P6 `0–1500` 秒的 M4C 产物；增量 ASR、帧和 Packet 只能从 1500 秒开始，旧 ID 不得重编号。完整 P6 只在 `merged/p6-complete/` 建立统一视图。
- M5 使用两段本地媒体维持完整绝对时间；canonical frame 是非破坏式 alias，旧 evidence 的 frame ID 必须继续解析，M4C 人工复核项不得因合并消失。
