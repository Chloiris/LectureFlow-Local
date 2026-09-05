# Reference Audit

## 当前结论

M0–M2 没有复制或运行任何用户列出的参考仓库代码，也没有把参考仓库作为依赖。当前字幕、
媒体、帧 Schema、选择器、解析器、缓存、状态和测试均独立编写。

## 候选产品参考

以下仓库仅被用户列为后续可做只读研究的候选；M0–M2 尚未抓取或审阅其源码，因此不对其许可
证或实现作未经验证的陈述：

- `JefferyHcool/BiliNote`
- `Rimagination/bili-note`
- `trans93589/course-video-to-obsidian`
- `le876/bilibili-obsidian-notes`

若后续确需复用少量代码，必须先记录精确文件、提交版本、许可证、版权归属、修改内容与无法
独立实现的理由，然后再引入。只借鉴抽象产品思路不等于复制代码，也会在此记录研究日期。

## yt-dlp 官方行为核对

2026-08-13 只读核对 yt-dlp 官方 `2026.07.04` 文档与源码，以确认 Bilibili 多 P 参数语义；
没有复制代码。结论是无 `?p` 的 anthology 默认返回 playlist，显式 `?p=N` 直接返回单视频，
因此不能对前者使用 `--no-playlist`，也无需对后者叠加 `--playlist-items`。

- 官方 CLI 选项：<https://github.com/yt-dlp/yt-dlp/blob/fdec00e0bf530dc6c3cc7b1dd780e95d9ae460e9/README.md#L482-L533>
- Bilibili 多 P 分支与测试：<https://github.com/yt-dlp/yt-dlp/blob/fdec00e0bf530dc6c3cc7b1dd780e95d9ae460e9/yt_dlp/extractor/bilibili.py#L415-L462>
- 许可证：yt-dlp 为 The Unlicense；本项目只依据公开行为编写独立 argv，没有复用其源码。

M1 另对项目环境中已锁定的 yt-dlp `2026.07.04` 做本地只读核对，确认其当前 Bilibili 字幕
实现使用 `/x/player/wbi/v2` 枚举 `need_login_subtitle` 和 `subtitle.subtitles`。LectureFlow 仅
据此确认端点/字段兼容性，HTTP 客户端、分类、下载、Schema 与缓存均独立实现；没有复制其
Python 代码。yt-dlp 作为项目依赖仅通过独立 CLI 进程用于元数据和受限媒体下载。

## ASR 兼容性预检（未安装）

2026-08-13 当前 uv 运行时为 CPython 3.12.13 / Apple Silicon。锁文件解析到
`mlx-whisper 0.4.3` + `mlx 0.32.0`（存在 CPython 3.12 macOS arm64 wheel），以及
`faster-whisper 1.2.1` + `ctranslate2 4.8.1`（存在 CPython 3.12 macOS arm64 wheel）。这只
证明依赖元数据可解析和目标 wheel 存在，不代表已安装或运行验收。M1 没有安装这些 extras，
没有下载模型；正式接入前仍须在隔离环境做小模型 smoke，并继续遵守 >2 GB 先确认规则。

## Microsoft VibeVoice-ASR-BitNet 可选运行时

2026-08-30 只读审阅微软官方 `microsoft/VibeASR.cpp` 提交
`5cbce71c65911a7e10639ac13b6ab6929e4c8f9e`、VibeVoice-ASR 模型卡和官方 BitNet 模型仓库。
官方代码与模型标注 MIT License。LectureFlow 没有复制其 C++/Python 源码，也不把仓库作为核心
依赖；只独立实现一个通过 argv 调用用户本地 `asr_infer` 的可选适配器。

审阅确认 pinned `prompt_builder.h` 把 `text` 明确标为 1.5B 输出，把含
`Start/End/Speaker/Content` 的 JSON 标为 7B 输出；微软 BitNet demo 同样固定 `text` 且单次最多
40 秒。因此适配器不宣称 1.5B 具备模型时间戳/说话人，而以最多 30 秒确定性 chunk 提供可审计的
粗粒度范围。

锁定模型 revision `66e78021ab8f5f06133d1ab421ba4d348bda97c9`，且只接受：

- `vibeasr-vae-encoder-i8_s.gguf`：703,080,064 字节，SHA-256
  `4941c82608c253ec066b5cc74d3dd11a5c8fef96cccbc5b87359ef0fe4338df6`
- `vibeasr-lm-i2_s-embed-q6_k.gguf`：992,877,600 字节，SHA-256
  `fbe273d8dc2f2433bb25f849e19d77ea65aaa2188d12c20cee987ab6f321e002`

官方模型仓库还包含超过 10 GiB 的 Safetensors，因此项目不执行整仓自动下载。当前提交只完成
接口、离线 fixture 和完整性门禁；尚未下载模型、编译上游 runtime 或宣称真实 M5 推理通过。

- 官方运行时：<https://github.com/microsoft/VibeASR.cpp>
- 官方模型：<https://huggingface.co/microsoft/VibeVoice-ASR-BitNet>
- 官方报告：<https://arxiv.org/abs/2607.21075>
