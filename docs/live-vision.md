# Windows 实时观察与视觉问答

需要 Windows 11 x64。源码运行需要 Python 3.12 x64。视频功能还需要可在 PATH 上找到的 FFmpeg 和 FFprobe，便携包也支持将两者放在可执行文件旁的 `ffmpeg/bin`。软件内问答使用显式指定的本机 Ollama 视觉模型。

```powershell
python -m pip install -r requirements-live.txt -r requirements-mcp.txt
python desktop.py --output runs/desktop --ollama-model qwen35:latest
python service.py --port 8769
```

从桌面程序的「实时观察与问答」进入控制台。它会连接现有 Eyes v1 服务，或者在本机启动服务。选择列表中的窗口可以按 HWND 锁定。选择「跟随前台」后点击「开始」，或者使用三秒倒计时锁定切换后的窗口。观察状态会显示最近目标、取证时间和临时不可用原因。关闭控制台会收起它，主窗仍可重新打开；「停止」结束当前观察，「暂停」结束服务上的所有观察并关停取证，退出主程序会结束它拥有的观察。

问答可以使用当前窗口最近一次有效观察，或使用已打开视频的当前采样区间和方法。历史区显示回答、观察 ID 和取证时间。视频证据支持查看拼图和清晰单帧，窗口证据可在图片有效期内打开。模型请求失败时可直接重试原问题。

## 本机契约

- v0 视频服务默认端口 8765，三个视频 MCP 工具和 PNG 路径继续兼容。
- 实时服务默认端口 8769，只监听 IPv4 上的 127.0.0.1。兼容本地 Host 和 Origin，请求不会启用跨域访问。除健康检查外，使用 `Authorization: Bearer` 或 `x-eyes-token`。
- 首次启动在 `%USERPROFILE%\.leviathan\eyes\token` 创建 48 位十六进制 token。Windows 使用当前用户的文件 ACL。使用已有 token 文件时请确认文件权限。可在同目录放置 `config.json`，或用 `--config` / `--token-file` 指定文件。配置示例为 `{"port":8769,"allowed_sources":["screen","window"],"denylist":["example.exe"]}`。显式添加 `camera` 后可以尝试读取第一台摄像头。
- 屏幕默认只采主屏。裁切坐标以来源左上角为原点。窗口经 Windows Graphics Capture 单独取帧，最小化、黑帧或取流失败时报告不可用。无法识别应用身份、或命中内置及配置的名称规则时跳过画面。主屏所选区域与这类可见窗口相交时也会跳过。当前名称规则和几何检查无法识别图像内部的所有敏感内容。
- `look` 支持临时 JPEG。图片最长边 1920 像素，默认 60 秒后无法读取。观察保留在进程内存，最多 64 条。watch 默认事件省略图片字段，仍可按观察 ID 在 TTL 内请求 JPEG。服务返回取证时间；无 OCR 和摘要模型时摘要为空。问句及 detail 作为兼容字段接受。
- watch 默认 0.5 FPS，画面低分辨率差异默认阈值 0.08，TTL 默认 600 秒。空 `window_ids` 表示跟随，`mode:"lock_current"` 表示把当前前台锁为目标。事件携带 boot ID 和游标。共用 token 的客户端默认共享两个 watch 配额，使用独立 `x-eyes-client` 名字可分别计数，总上限四个。

## DSH 与桌宠

在 DSH 的 MCP 管理界面添加 stdio 服务器。命令为本机 Python，参数依次为 `mcp_connector.py` 的绝对路径、`--port`、`8765`。使用便携包时命令为 `LeviathanEyes.exe` 的绝对路径，参数为 `mcp --port 8765`。连接器默认从用户目录读取 token，也接受 `LEVIATHAN_EYES_URL` 与 `LEVIATHAN_EYES_TOKEN`。DSH 需启用对应工具并选用接受图片工具结果的模型。

系列桌宠可在用户启用桌面感知后使用 HTTP 或 MCP。BailongPet 的 DSH 对话入口需独立验收。

### 按会话保留证据和发问

有会话归属的实时观察、watch 及事件都要求专用凭据。先在 Eyes 服务机器上注册会话：

```powershell
python service.py --register-session $sessionId
```

该命令在 Eyes 配置目录的 `sessions` 中生成独立凭据。HTTP 客户端使用 `x-eyes-session` 和该会话的 Bearer token。知道观察 ID 或主 token 也无法读取另一会话的元数据和图像。普通 MCP 使用主 token，走原有公共观察池。删除某会话的凭据文件会立即拒绝该会话后续的 HTTP 请求。

只有当宿主为每条会话隔离 MCP 实例和工具可见范围时，才对该实例传 `--session-id $sessionId`（也可用 `DSH_SESSION_ID`）。凭据从专用文件或 `LEVIATHAN_EYES_SESSION_TOKEN` 读取。DSH 的全局 MCP 注册应保持无 session 参数。把 A 的会话参数挂在全局连接器上，会同时把 A 的凭据交给其他可访问该工具的会话。

针对允许外部客户端连接的本机 **DSH Web**，把其启动地址保存在 Eyes 桌面进程的 `DSH_WEB_LAUNCH_URL` 环境变量。启动地址包含访问凭据，按密码处理。打开 Eyes 控制台，粘贴完整会话 ID 并点击「绑定 / 重连」。Eyes 会先确认该会话存在和有权读写对应证据，再允许向指定 ID 发送带图片和观察 ID 的问题。对已发送但结果暂未确认的问题，重试只等待原请求。发送后的附件由 DSH 按该会话的历史与模型路由处理，Eyes 临时图的 TTL 只适用于 Eyes API。

### DSH Desktop 插件模式

DSH Desktop 没有 `DSH_WEB_LAUNCH_URL` 时，Eyes 控制台会改用 `dsh_plugin.py`：
它读取 `~/.leviathan/eyes/plugin.token`，向 `http://127.0.0.1:7351` 的
`leviathan-eyes` DSH 插件发送 `POST /v1/ask`，并通过 `GET /v1/reply` 取回该会话的答复。
插件工具本身使用当前 DSH session 的 `x-eyes-session` 凭据，不需要浏览器 cookie。
可用 `DSH_PLUGIN_URL` 和 `DSH_PLUGIN_TOKEN_FILE` 覆盖默认地址与令牌路径。

DSH 桌面端向全局 Eyes MCP 发问的链路独立可用。已有 Desktop Host 需要额外提供受支持的外部授权入口，以及能传递可信调用会话身份的 MCP 注册方式，才能在桌面 A/B 验收中启用上述私有证据链；插件模式即该入口的 P0 实现。


## 构建与验证

```powershell
python -m pip install pyinstaller -r requirements-live.txt -r requirements-mcp.txt
pwsh -File scripts/build_windows.ps1 -Output runs/windows-build
python scripts/verify_live.py runs/live-check --native
python scripts/verify_http.py runs/http-check
python scripts/verify_desktop.py runs/desktop-check --method change_nested
```

便携包 `LeviathanEyes-win-x64.zip` 解压后启动 `LeviathanEyes.exe`。可选命令为 `service --port 8769`、`mcp --port 8765`、`desktop --ollama-model qwen35:latest`。调用窗口验证时脚本会打开两个纯色测试窗口并切换焦点。任何保存的实时验收图片都来自这两个测试窗口。
