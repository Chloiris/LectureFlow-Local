# Privacy and Security

- 课程媒体、字幕、帧、Packet、分析和笔记只保存在本地任务目录。
- 不实现或调用任何外部模型 API，不要求 API Key。
- 网络仅用于用户指定的视频/字幕来源、正常软件依赖与获准的本地模型下载。
- 不复制 Chrome Cookie 数据库，不读取钥匙串。需要登录态时只接受用户显式指定的
  `cookies.txt`，并检查其权限；Cookie 内容永不打印或写入 manifest。
- 日志对 Cookie、Authorization、token、密码和常见签名查询参数脱敏。
- M1 B站字幕请求不读取或发送 Cookie；字幕下载只接受已知 Bilibili/HDslb/Bilivideo HTTPS
  域，防止服务端字幕 URL 把客户端引向任意主机。
- HTTP 缓存元数据中的 URL 会脱敏；原始 API 响应只存本地 `workspaces/`，不会提交 Git。
- ASR 能力检测不联网。默认 MLX 模型只使用已审计缓存；可选 VibeASR 必须由用户显式配置项目外
  `asr_infer` 和两份固定 GGUF。任一缓存缺失都拒绝执行，不自动下载、编译或回退云端。
- M2 B站媒体只从用户指定来源下载，限制处理范围与 1080p；估算超过 500 MiB 时下载前停止。
- M2 不把帧发送给任何服务、不做模型视觉理解或 OCR；FFmpeg/Pillow 是唯一图像处理组件。
- 媒体/帧正式 artifact 只保存任务相对路径；本地源的个人绝对路径不会复制到这些产物。
- 任务目录默认权限为当前用户可访问；`workspaces/` 被 Git 忽略。
- Obsidian 导出仅能写入用户显式指定的目标目录；生成文件带受控标记，遇到非生成文件或人工
  修改会拒绝静默覆盖。项目不会自行扫描或写入其他 Vault。
- 本地 Web 服务拒绝非 loopback host，且不使用 CDN。
