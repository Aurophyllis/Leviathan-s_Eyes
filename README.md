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
| `POST /v1/videos/{video_id}/observations` | 传入 JSON 的起止毫秒、`k` 和 `method`，返回按时间排序的帧、单帧 URL 和拼图 URL。 |
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

## 第四步 · 画面变化驱动选帧

桌面界面可切换「均匀」「画面变化」「变化与覆盖」「嵌套覆盖」。CLI、HTTP 和 MCP 默认使用 `uniform`。

- `uniform` 按等间隔时间点选取附近帧。
- `change_only` 在 64×36 缩略帧上计算 HSV 变化分，依次优先硬切、累积运动和静止兜底，同类触发按分数降序。首遍避免相邻帧过近，次遍补足预算。
- `change_guarded` 先预留约四分之一预算作为均匀时间锚点，再使用同一变化优先级补齐。首版的锚点会随预算重算，较大预算下的选帧集合可能变化。
- `change_nested` 每四个选帧位置预留一个时间锚点。锚点按区间中点、四分之一、四分之三的顺序逐级细分，其余位置按变化优先级补入。同一视频和观察区间中，较大的预算会保留较小预算选出的所有帧。

变化驱动的观察目录额外保存 `sampling_trace.json`，含分数、触发原因及阈值。三种方法都使用同一原始帧时间轴。变化方法目前会扫描指定区间的缩略帧并单独解码入选原帧，长视频扫描开销需要另行实测。

```powershell
python app.py sample.mp4 --k 4 --method change_guarded --output runs/guarded
python app.py sample.mp4 --k 8 --method change_nested --output runs/nested
python scripts/evaluate_sampling.py runs/fixed-budget-evaluation
python scripts/verify_desktop.py runs/guarded-desktop-check --method change_guarded
```

合成评测固定四个样片，在相同区间和帧数下比较证据命中。时间戳落入人工定义的证据区间即记一次命中。同一场景的重复帧不额外加分。

## 第五步 · AKS 选帧逻辑离线复现

从[资源索引](docs/phase-1/README.md)取得固定版本的 AKS 源码 ZIP。下面的可选验收需要 NumPy，以执行已核对哈希的作者脚本。本项目的离线选帧实现只使用标准库。

```powershell
python -m pip install numpy
python scripts/reproduce_aks.py 'C:/path/to/AKS-source.zip' runs/aks-blip
```

使用作者缓存的 LongVideoBench BLIP 分数和帧序号，按 32、64 两个预算逐题对比完整选帧列表。脚本保存本地实现的输出、作者脚本复跑结果、输入哈希，以及与仓库随附帧列表分别比较的结果。此步骤验证给定分数后的选帧逻辑。视频解码、BLIP 推理与下游问答准确率需要独立验证。

## 第六步 · FOCUS 同题对照

从[资源索引](docs/phase-1/README.md)取得固定版本的 FOCUS 源码 ZIP。离线脚本校验 `focus.py` 的哈希后调用作者的粗筛、细筛和最终分配，并独立重算每个片段的粗筛置信分。该实验使用固定随机种子和合成题目对应的白色形状模板评分器。时间标注仅用于评估。

```powershell
python -m pip install numpy scipy decord
python scripts/evaluate_sampling.py runs/fixtures
python scripts/compare_methods.py 'C:/path/to/FOCUS-source.zip' runs/fixtures runs/paired-4 --k 4 --ollama-model qwen35:latest
python scripts/compare_methods.py 'C:/path/to/FOCUS-source.zip' runs/fixtures runs/paired-8 --k 8
```

五组使用相同视频、题目、输入帧数和拼图设置。AKS 全量组对每帧打分，AKS 匹配组用均匀候选将打分次数限制为该题 FOCUS 的实测值。短预算将 AKS 深度上限设为 2 或 3，保留每个末级片段至少一帧。均匀与嵌套选帧无需调用题目相关性评分器，后者仍有画面变化扫描成本。

仓库保留复跑工具。实验目录生成逐题证据覆盖、粗细阶段打分记录、视觉模型回答和 PNG。相关性评分次数单独计数，预解码缩略图及画面变化扫描不计入该指标。这套合成实验未使用 BLIP 或真实视频问答基准。

## 第七步 · Video-MME 真实视频小样本

输入采用 [Video-MME](https://huggingface.co/datasets/lmms-eval/Video-MME) 原视频与题目，并按题目 ID 关联 [KFS-Bench](https://github.com/NEC-VID/KFS-Bench) 的 Video-MME 场景标注。下载脚本按 ZIP 字节范围取得指定视频，输出可核对的视频哈希。外部视频、标注和模型权重保存在本地实验输入目录。

安装试验依赖，并下载 [Xenova CLIP ONNX 固定版本](https://huggingface.co/Xenova/clip-vit-base-patch32/tree/6b7c523703ae26e39be4b03774966f8571c7f523) 的 `onnx/model_quantized.onnx`、`tokenizer.json` 和 `preprocessor_config.json` 放到同一目录。权重 SHA-256 应为 `0898a3facfdb27f0a041e57649b4989cfd094e4a0040d6ae75ed69917dfc7328`。Windows 运行时也需要 FOCUS 作者实现的 SciPy 和 Decord 依赖。

```powershell
python -m pip install numpy scipy decord pyarrow onnxruntime tokenizers
python scripts/fetch_videomme_subset.py path/to/questions.parquet path/to/KFS-Bench-source.zip runs/videomme-short --chunks 20 --videos 3 --max-mib 48 --groups short
python scripts/fetch_videomme_subset.py path/to/questions.parquet path/to/KFS-Bench-source.zip runs/videomme-medium --chunks 12 --videos 1 --max-mib 100 --groups medium
python scripts/fetch_videomme_subset.py path/to/questions.parquet path/to/KFS-Bench-source.zip runs/videomme-long --chunks 9 --videos 1 --max-mib 60 --groups long
python scripts/evaluate_real_video.py path/to/clip path/to/FOCUS-source.zip runs/real-pilot runs/videomme-short/manifest.json runs/videomme-medium/manifest.json runs/videomme-long/manifest.json --candidate-cap 64 --k 4 --ollama-model qwen35:latest
```

Video-MME 题目文件来自 [固定版本 parquet](https://huggingface.co/datasets/lmms-eval/Video-MME/resolve/ead1408f75b618502df9a1d8e0950166bf0a2a0b/videomme/test-00000-of-00001.parquet)。每个原视频在统一候选时间轴上评测，CLIP 使用题干与帧的余弦相似度。FOCUS 按需请求相关性分数，按候选序号分臂。AKS 使用与本题 FOCUS 相同的打分预算。嵌套覆盖和均匀采样共用候选池。所有方法使用相同的四格拼图排版进行纯视觉问答。

输出记录场景命中、候选池覆盖上限、相关性评分次数和分段耗时。结果以小样本试验为单位，真实评测时长、题目分布和候选池密度均应随结果报告。
