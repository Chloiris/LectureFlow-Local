# AGENTS.md

## 开发不变量

- 禁止任何外部模型 API；不要添加要求 API Key 的依赖或调用。
- 不要修改当前仓库之外的 BiliNote、Obsidian Vault、浏览器数据库或钥匙串。
- 不要提交新生成的 `workspaces/` 任务内容、媒体、音频、模型、缓存、Cookie、日志、个人路径或
  用户笔记；真实 M3B 产物留在被忽略工作区，只提交实现、测试和项目文档。
- 原始字幕与原始帧不可覆盖；派生数据写新文件并保留证据 ID。
- 所有 subprocess 使用 argv 列表、`shell=False`、超时和脱敏错误。
- 超过 2 GiB 的模型下载必须先获得用户确认；一次任务只允许一个明确选定的本地模型。
- B站视频预计超过 500 MiB 时必须先获得用户确认；不要用抽帧配置变化触发重复媒体下载。
- M2 只允许确定性媒体/图片处理；没有进入 M3 时不得读取关键帧做语义分析或运行 OCR。
- M4A 不得重新运行 ASR或生成词级时间戳；段落边界必须来自原始 segment，raw / cleaned /
  corrected 分层，pending 纠错不得应用。
- M4B 仅处理显式 P6 0–1500 秒并复用固定 medium 模型；5 个 Packet 必须串行，overlap 只能作
  context。公式必须绑定高清 frame，低清符号不得补全；合并不得重新读取原图。
- M5 必须把 P6 0–1500 秒视为冻结源；只处理 1500 秒以后并按旧最大 ID 追加。旧 frame 只能建 canonical alias，不能删除或重编号；完整 P6 只能在合并目录生成。
- VibeASR BitNet 只能作为显式本地可选后端；不得自动克隆、编译或下载。必须锁定官方 revision、
  二进制/两份 GGUF 哈希和 24 kHz 独立音频缓存，同一 job 不得切换 backend 覆盖 raw。
- Web 只能绑定 `127.0.0.1` 且无 CDN；Obsidian 只能写用户显式目录并保护非生成/手工修改文件。

## 开发命令

项目必须通过 uv 使用 Python 3.11+；本机裸 `python3` 可能过旧。

```bash
uv sync --dev --locked
uv sync --dev --extra asr-mlx --locked  # 仅在本地 ASR 工作明确授权时
uv run ruff check .
uv run pytest
uv run pytest --cov=lectureflow --cov-fail-under=87.86
uv run lectureflow doctor
```

测试必须离线运行。需要媒体的 smoke fixture 应由仓库脚本用 FFmpeg 生成，不提交二进制视频。

## 处理任务

1. 运行 `lectureflow doctor`，不要自动安装缺失工具。
2. `prepare` 注册来源；相同输入应命中缓存。
3. 用 `status` 检查最后成功阶段；失败后用 `resume`，不要全量重跑。
4. 按字幕优先级处理，已有可靠字幕时禁止重复 ASR。
5. M2 用 `media acquire` 与 `frames extract` 生成确定性清单；重复运行必须命中缓存，单帧损坏
   优先局部修复。
6. 生成 Packet 后，先读联系表筛选，再逐张读取入选的高清帧；这是 M3 行为，M2 不得提前做。
7. 将 Codex 结果写入 `analyses/*.json`，运行 `validate` 后才能渲染证据和笔记。
8. 验收视觉结论是否包含 frame ID/时间，文字结论是否包含 segment ID/时间。
9. M3B 先运行 20–30 秒 MLX 预检；原始 ASR 永不语义改写，`speech+slide` 结论必须引用两类 ID。
10. 严格 offline 缺模型应失败；禁止云端 ASR、外部模型 API、OCR、子 Agent 和 `codex exec`。
11. M4A 用 `paragraphs build`、`corrections build`、`render` 校验后再 `serve` 或导出；页面跳转
    粒度是段落开头，不应声称逐词/逐句对齐。
12. M4B 用 `packets build/validate` 后逐包写入并校验 `analyses/packets`；`merge` 只读已验证结构化
    结果。缓存/resume 测试不得通过重新下载或覆盖原始产物完成。
13. M5 先冻结再处理增量 job；每个新增 Packet 独立缓存。最终复核冻结哈希，确保旧 ASR、帧、Packet、公式审计和人工队列均未变化。

## 完成标准

不得在真实五分钟烟雾测试通过前宣称项目完成。每个里程碑更新 `PROJECT_STATUS.md` 和
`CHANGELOG.md`，运行 ruff 与 pytest，并创建一个范围清晰、可验证的提交。
