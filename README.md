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
