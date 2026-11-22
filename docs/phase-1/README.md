# 第一阶段资源索引

项目由 Luo 单人维护。通过 GitHub 记录代码、配置和已核验的实验输出，复跑时检出对应提交。[单人实施计划](Luo-第一阶段任务书.md)

样片清单记录样片 ID、来源、生成或下载方式、SHA-256、题目与必要证据区间。自制样例可随项目版本保存。外部素材保留可核对的官方来源。实验记录关联样片版本、代码提交、参数和实际运行命令。

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
