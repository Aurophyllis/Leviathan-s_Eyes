# 第一阶段协作与资源索引

Luo 和 Chen 在各自电脑检出同一个 GitHub 仓库。仓库中的任务书、样片清单和验收资料使用相对路径。每个人在自己的电脑运行应用和本机 HTTP 接口。

- [Luo 的任务书](Luo-第一阶段任务书.md)
- [Chen 的任务书](Chen-第一阶段任务书.md)

```powershell
git clone https://github.com/Aurophyllis/Leviathan-s_Eyes.git
Set-Location Leviathan-s_Eyes
```

## 共享与验收

| 负责人 | 在 GitHub 交接的内容 |
|---|---|
| Luo | 使用 `phase1/luo-app` 分支提交接口示例、四帧固定样例的格式与预期输出、启动命令，以及去除凭据的 Agent 演示记录。 |
| Chen | 使用 `phase1/chen-sampling` 分支提交选择器、样片清单、小样例或生成脚本、阈值配置和实验结果。 |
| 两人 | 通过 PR 写清可复跑命令、样片 ID、预期结果和版本依赖。对方在自己电脑检出该提交，跑过以后在 PR 记录结果。缺少仓库写权限时可从 fork 提交 PR。 |

小型自制或允许再分发的样例可随 PR 提交。对较大的可再分发样片，可用 GitHub Release 附件；仅供研究或分发权限不明的素材在清单中列原始下载地址或生成命令。样片清单建议放在 `experiments/fixtures/manifest.csv`，至少记下样片 ID、原始来源、取得方式、SHA-256、时长，以及按视频起点计时的必要证据区间。四帧样例还应记录输入时间戳、预算和预期索引。实验记录在 PR 中关联样片版本、代码提交、参数和命令。

## 参考资源下载和核对

在各自电脑选一个本地目录保存下载文件。源码以固定提交为准，可直接下载 ZIP，或克隆作者仓库后检出表中提交。ZIP 解压后，按表检查入口和数据。若使用 Git 克隆，可用 `git rev-parse HEAD` 核对提交。

| 资源 | 下载地址 | 核对标准 |
|---|---|---|
| dsh-see-now | [固定源码 ZIP](https://github.com/SAOS-3624/dsh-see-now/archive/59c4d1a1b4bab0f6efec896f8bed578c352aca23.zip) | 提交 `59c4d1a1b4bab0f6efec896f8bed578c352aca23`，检查 `src/cuts.js`、`src/pipeline.js`、`src/sheet.js`、`LICENSE` 和 `NOTICE`。 |
| AKS | [CVPR 2025 PDF](https://openaccess.thecvf.com/content/CVPR2025/papers/Tang_Adaptive_Keyframe_Sampling_for_Long_Video_Understanding_CVPR_2025_paper.pdf) · [固定源码 ZIP](https://github.com/ncTimTang/AKS/archive/b0b8a58fedf1d05d78151e2969cecde60e83721d.zip) | 核对题名 *Adaptive Keyframe Sampling for Long Video Understanding* 和源码提交 `b0b8a58fedf1d05d78151e2969cecde60e83721d`，检查 `frame_select.py` 与 `outscores/longvideobench/blip/`。 |
| FOCUS | [ICLR 2026 PDF](https://openreview.net/pdf?id=1OQKqLFcbB) · [arXiv 备用](https://arxiv.org/abs/2510.27280) · [固定源码 ZIP](https://github.com/NUS-HPC-AI-Lab/FOCUS/archive/d469757cd89976117467294fd1f177026d1a627d.zip) | 核对题名 *FOCUS: Efficient Keyframe Selection for Long Video Understanding* 和源码提交 `d469757cd89976117467294fd1f177026d1a627d`，检查 `focus.py` 和 `select_keyframe.py`。记录所用论文版本。 |
| K-frames | [arXiv v1 PDF](https://arxiv.org/pdf/2510.13891v1) | 核对 arXiv `2510.13891v1` 和题名 *K-frames: Scene-Driven Any-k Keyframe Selection for Long Video Understanding*。阶段一阅读其片段选取与训练设计。 |
| KFS-Bench | [WACV 2026 PDF](https://openaccess.thecvf.com/content/WACV2026/papers/Li_KFS-Bench_Comprehensive_Evaluation_of_Key_Frame_Sampling_in_Long_Video_WACV_2026_paper.pdf) · [固定源码和标注 ZIP](https://github.com/NEC-VID/KFS-Bench/archive/2349b9ebe00433db250f28cac497b15dbd1ca48a.zip) | 核对源码提交 `2349b9ebe00433db250f28cac497b15dbd1ca48a`，检查 `eval.py`、`lvb_anno.csv` 和 `vidmme_anno.csv`。视频来源另查对应基准。 |
| Video-MME 与 LongVideoBench | [Video-MME 官方说明](https://github.com/MME-Benchmarks/Video-MME) · [LongVideoBench 官方说明](https://github.com/longvideobench/LongVideoBench) | 用正式数据版本与视频、题目 ID 对齐。涉及 KFS-Bench 时，额外核对视频起点、时间戳单位和标注对应关系。 |

如需核对与 Luo 当前持有的论文是否为同一份文件，可执行 `Get-FileHash -Algorithm SHA256 -LiteralPath './下载文件.pdf'`。下表用于逐字节比对，使用其他公开版本时记录版本与实际哈希。

| 本地基准文件 | SHA-256 |
|---|---|
| AKS-CVPR2025.pdf | `3F674C0C5C40E2622F2F697C2FD9E0600B613EC7D30A31B9C4FDEB1EAEBE41D6` |
| FOCUS-arXiv-2510.27280.pdf | `A4B03F962CF396A7AE605A8DA5E7D1762E06176F0BFB28FCE56B661DD91DDF8C` |
| K-frames-arXiv-2510.13891.pdf | `62D9838BC548AF087859FEB6BDE735EB00C784C23D9D6E994FDB8CE996545D0B` |
| KFS-Bench-WACV2026.pdf | `D2F286DE2DE71FE3EFA40418B7963275F6E10BA156645A1BA35D8EACAEAEEB33` |
