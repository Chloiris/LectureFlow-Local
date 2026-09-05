# M1.5 Real Bilibili Subtitle Smoke

日期：2026-08-13

测试来源：`https://www.bilibili.com/video/BV1pf421z757/`

## 结论

该课程共有 16 个分 P。公开无 Cookie 请求得到以下逐 P 分类：

- E（需要登录态）：P1–P5、P7–P16，共 15 P；接口明确返回
  `need_login_subtitle=true`，候选数组为空。
- D（接口报告确实无字幕）：P6，共 1 P；接口返回
  `need_login_subtitle=false` 且候选数组为空。

因此本次不能宣称真实字幕获取成功，也不能进行五段字幕原文/时间戳抽查。没有下载字幕
JSON，没有生成该真实任务的 JSONL/TXT/SRT/VTT/质量报告，也没有安装 ASR 或读取 Cookie。
这不是 C：当前证据没有证明页面存在可见 AI 字幕但公开接口无法取得。

## 实际识别

- BVID：`BV1pf421z757`
- aid：`1206079918`
- 分 P：16
- P1 cid：`1649019286`
- P6 cid：`1640319869`
- job ID：`bili-bv1pf421z757-all-118b886a`

完整分 P/cid/时长/标题保存在任务的
`raw/subtitles/subtitle-selection-report.json`，运行时目录不提交 Git。

## 缓存与离线验证

首次真实请求后，使用 `--offline` 从已缓存的 17 份 API 响应完成分类重建；之后普通 fetch
和 `resume` 均返回 `cache_hit=true`，没有访问字幕下载地址，下载数为 0。严格离线缺少元数据
缓存时以 `offline_cache_miss`/`Offline metadata cache miss` 明确失败。

重跑前后关键哈希保持一致：

- selection report：`a5d9c4495975c083079187ed9553cd19bf995bd787b17a1bfa0687108dc9cd2b`
- subtitle result：`94189147dcbb080e1d73abff861092f72aecf33cb62d7b017c54445bc243359e`
- HTTP cache 文件集合：`610564f780591ec7534f09a8d525d78aae6f06051b298fbb398759c9d7a04eb7`

真实字幕阶段状态为 `subtitle_ready=succeeded`、`transcript_ready=pending`。这表示候选枚举已
确定性完成，但没有可公开转换的字幕；不会把结果伪装成 transcript ready。
