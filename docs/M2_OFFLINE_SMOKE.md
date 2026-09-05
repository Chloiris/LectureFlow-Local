# M2 Offline Smoke

日期：2026-08-13

测试使用 `tests/fixtures/generate_m2_video.py` 在 pytest 临时目录动态生成 H.264 MP4，不提交
二进制媒体。32 秒画面包含标题、PPT、公式、代码、黑屏、非相邻重复公式页、长静态页和总结页；
测试路径同时包含中文与空格。测试为任务写入带时间的本地字幕 fixture，以验证“看这里”、
“这个公式”、“接下来演示”和“界面上”等提示词及 `segment_id` 关联。

验收项：

- FFprobe 媒体清单、外部本地源只读复用和完整 SHA-256。
- FFmpeg 场景变化、字幕提示词前后偏移、周期安全采样三个来源均产生候选。
- 黑区间被抽为质量探针并由像素指标标为黑帧，不进入最终帧。
- 非相邻重复画面由全局 64 位 dHash 决策并记录 canonical candidate。
- 最终帧严格受预算控制；另用 36 秒动态 test source 验证可配置硬上限。
- 联系表每页不超过 12 帧，frame ID/时间标签和 manifest 一致。
- 同配置第二次媒体和抽帧均命中缓存；修改帧配置只重跑 frames，不改变媒体清单字节。
- 模拟 running attempt 后恢复：旧尝试标记 recoverable，新尝试成功。
- 删除单张最终 PNG 后仅按其时间点修复该帧，并重建联系表与 artifact manifest。
- 所有帧时间落在请求范围内，所有 artifact 路径保持任务相对路径。

对应自动化测试：`tests/smoke/test_m2_offline_pipeline.py`。整个过程无网络、无 Cookie、无 ASR、
无模型下载、无外部模型 API、无视觉语义分析或 OCR。
