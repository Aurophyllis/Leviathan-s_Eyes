# Leviathan's_Eyes

面向 Windows 的本地视频观察软件与自适应关键帧采样课设。

- [单人实施计划](docs/phase-1/Luo-第一阶段任务书.md)
- [参考资源与版本核对](docs/phase-1/README.md)

## 第一步 · 均匀取帧

需要 Python 3，以及在 PATH 中可运行的 `ffmpeg` 和 `ffprobe`。在仓库根目录运行。

```powershell
ffmpeg -f lavfi -i "testsrc2=duration=4:size=320x180:rate=10" -c:v mpeg4 sample.mp4
python app.py sample.mp4 --start-ms 1000 --end-ms 3000 --k 4 --output runs/first
Get-Content runs/first/observation.json
```

该样例应产生四张 PNG，时间戳依次为 1200、1700、2200、2700 毫秒。输出目录需要是新目录。命令按视频首个有效展示时间戳计时，区间左闭右开，时间相同的帧只计一次。候选不足时 JSON 记录实际帧和 `shortfall`。

首步 CLI 使用 FFprobe 枚举视频帧，再用 FFmpeg 按序号取回 PNG。它用于验证时间戳与均匀基线，长视频的按区间解码优化安排在后续阶段。

## 第二步 · 本机观察接口

安装拼图依赖后启动服务。服务监听 `127.0.0.1`，每次启动时指定本次观察的视频。

```powershell
python -m pip install -r requirements.txt
python serve.py sample.mp4 --output runs/server --port 8765
```

| 请求 | 返回 |
|---|---|
| `GET /v1/videos/current` | 视频 ID、宽高和时长。 |
| `POST /v1/videos/{video_id}/observations` | 传入 JSON 的起止毫秒、`k` 和 `method: "uniform"`，返回按时间排序的帧、单帧 URL 和拼图 URL。 |
| `GET` 返回的图片 URL | PNG 图像字节。拼图格号与单帧记录的 `frame_id` 一致。 |

空区间指合法时间段内没有候选帧，此时返回空帧列表、缺额和空的拼图 URL。进程重启后使用新的会话 ID，已产生的 PNG 与 JSON 保留在输出目录。

运行下方命令可生成测试视频并执行实际 HTTP 验收。请为每次运行指定新目录，保留视频、请求响应、拼图、单帧、服务器日志和核验 JSON，方便课程报告追溯。

```powershell
python scripts/verify_http.py "runs/http-check"
```

## 第三步 · Windows 窗口与 MCP 连接器

使用带 Tcl/Tk 的 Python 3.11 或更新版本。可以运行 `python -m tkinter` 检查 Tk 窗口是否可用。

```powershell
python -m pip install -r requirements.txt -r requirements-mcp.txt
python desktop.py --output runs/desktop --port 8765
```

在窗口内打开视频，填入起止毫秒和帧数，点击「生成拼图」。选中右侧列表中的任一帧，可预览对应原始 PNG。「返回拼图」恢复总览。

在支持 MCP stdio 且能向视觉模型传递图片工具结果的宿主中添加连接器。将命令和脚本路径填写为本机绝对路径。桌面程序与连接器使用同一端口。

```json
{
  "mcpServers": {
    "leviathan-eyes": {
      "command": "C:/path/to/python.exe",
      "args": ["D:/path/to/Leviathan's_Eyes/mcp_connector.py", "--port", "8765"]
    }
  }
}
```

Agent 先调用 `current_video`，再用 `observe_video` 获取带编号的拼图和时间戳。根据返回的观察 ID 与格号调用 `get_frame`，取得原始 PNG。软件在本机记录实际的拼图和观察 JSON。关闭窗口会停止本机服务。

### 验收复跑

脚本会打开并关闭窗口，保存窗口截图、MCP 返回的 PNG、工具调用结果与测试样片。若本机 Ollama 有支持视觉的模型，可追加模型选项，记录它从拼图选择格号、再查看原始帧后给出的实际答案。

```powershell
python scripts/verify_desktop.py runs/desktop-check
python scripts/verify_desktop.py runs/desktop-vision-check --ollama-model qwen35:latest
```
