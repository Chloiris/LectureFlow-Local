# M2 Real Five-minute Smoke

日期：2026-08-13

## 范围与边界

- 来源：`https://www.bilibili.com/video/BV1pf421z757/`
- 任务：`bili-bv1pf421z757-all-7073c3d8`
- 媒体策略：根多 P URL 在 M2 默认选 P1；`part_id=BV1pf421z757_p1`。
- 时间范围：00:00–05:00（300 秒）。
- 无 Cookie、无 ASR、无模型下载、无外部模型 API、无 OCR、无视觉语义分析。
- 该来源无登录态下 P1 字幕分类 E，因此真实提示词候选为 0；没有伪造字幕或提示命中。

## 媒体结果

- 下载前估算：24,013,605 bytes（22.9 MiB），低于 500 MiB 门禁。
- 实际文件：34,077,644 bytes（32.5 MiB）。
- 300.0 秒，1920×1080，H.264，含音轨。
- 媒体 SHA-256：`84569131437b9bd4d4da8c97a3b8c036ed73e0325d73d3d46d044214b486227b`。
- yt-dlp `2026.07.04`；FFprobe `8.1.2`。

## 抽帧结果

- 场景/黑区间候选：15；提示词候选：0；周期候选：4。
- 合并并实际抽取：19；全局 dHash 重复丢弃：12；质量丢弃：0；最终：7。
- 最终来源计数：`scene_change=6`、`periodic=4`（同一帧可同时携带多个来源）。
- 最终时间点：0.0、52.3、65.2、204.4、220.9、252.4、270.566667 秒。
- 最大最终帧间隔（含边界）：139.2 秒。周期候选未丢失；静态画面的周期帧在候选账本与
  去重映射中保留，但与更优候选合并。
- 联系表：1 张、7 帧，未超过每页 12 帧；最终帧未超过预算 30。

## 缓存与恢复

- 第二次 `media acquire` 命中缓存，视频 SHA-256 不变，没有重复下载。
- 第二次 `frames extract` 命中缓存；正式媒体清单、帧 artifact manifest、frames JSONL 和
  覆盖报告 SHA-256 完全不变。
- `resume` 命中 `frames_ready` 缓存；`status` 显示最近成功阶段为 `frames_ready`。
- 补录多 P 选择策略时只更新媒体清单并最小失效 frames；媒体文件未重新下载。
- 开发期间把媒体执行器从全局 console script 切换为最终的项目解释器 `python -m yt_dlp` 时，
  工具身份缓存键按设计变化并额外下载过一次同一 32.5 MiB 片段；文件 SHA-256 相同。最终实现
  固定后再次运行已命中缓存，不再下载。
- 主动中断一次真实 `frames_ready` 后，`resume` 将旧 attempt 标为 recoverable 并成功追加新
  attempt；随后再次运行命中缓存。

## 关键审计哈希

```text
media/media-manifest.json               fe988f197dc9b8361e3fc18d867f173ff863da9b949145401963a3a739413fff
frames/frame-artifact-manifest.json     57dbfb9fcafff464c352790c3d6a07f504c162a1fbf0f094f172eb461334e3b8
frames/frame-candidates.jsonl           3f2ef931b6bf321573d4a367fcb37ea940afa8232c99483f06b1e510a4d0f30c
frames/frames.jsonl                     d2610f3da28501594e29a449d2da8ec7135b25459497c2f7e80b81a15c275602
frames/frame-dedup-report.json          9301c5a67e1e273ed6bd1e3e820603b0c1a270ccd8094900ca3ed2cf89c4606f
frames/frame-quality-report.json        3a656565cfbf7ed0f16fa89ffe52d12bf0b09ac986d58156bb7144a12d445d3d
frames/cue-matches.json                 38b19266c5d454c0080359e3b5741f700cec9c6d1d51eda84da9300a35be133c
frames/contact-sheet-manifest.json      894952ac6e0be78312337fd4abdfb6fb08a4c513c0275b77a53f6962cdbdc1d2
frames/contact-sheets/contact-sheet-001.jpg 5d8bb028c5807792fa73cc26113b2e5f3f7cf6c6bbe47c173e800309b7501fb0
reports/frame-coverage-report.json      ec4ff421372187ff5ffe01f4dab77eef34d02355368f3502a329c72cfb209553
```

任务数据位于 Git 忽略的 `workspaces/`，未提交媒体、帧、缓存或个人路径。此验收只证明 M2，
不证明 M3–M8 或整个产品完成。
