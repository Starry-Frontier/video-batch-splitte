# 视频分割转码

[![Tests](https://github.com/Starry-Frontier/video-batch-splitte/actions/workflows/tests.yml/badge.svg)](https://github.com/Starry-Frontier/video-batch-splitte/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)

通过图形界面或命令行，可以按单一或多种时长批量分割视频，并为每个源视频输出多套分辨率方案。最后不足设定时长的内容会作为最后一个片段保留。快速无损模式保留源编码和容器，精确模式输出 H.264/AAC MP4。

## 功能

- 可在文件夹中单选/多选具体视频，也可一次导入整个文件夹及其子文件夹
- 支持单个时长以及 `3, 6, 10` 这类多时长批量方案
- 支持原始分辨率、常用预设及自定义“宽x高”批量分辨率方案
- 支持逐个修改自定义名称，或对选中的视频应用批量命名规则
- 默认使用无损流复制，保留原始视频和音频编码
- 无损模式根据文件内容识别真实容器并使用正确扩展名
- 两种模式优先使用源文件标记的默认视频轨。快速无损模式保留该视频轨、音频、字幕和元数据，排除非播放数据流。
- 支持统一命名模板并自动补齐三位序号
- 每次运行创建新的结果目录，不覆盖旧文件
- 在后台检查源编码兼容性，处理后验证片段可识别性、时长和无损视频数据包数量
- 分割失败时自动清除本次产生的0字节文件和空目录
- 实时进度、取消处理、单个文件失败后继续处理其余文件
- 同时支持图形界面和命令行

## 环境要求

- Python 3.8 或更高版本（图形界面使用 Python 自带的 Tkinter，无需安装 pip 依赖）
- [FFmpeg](https://ffmpeg.org/download.html)，建议同时带有 `ffprobe`

程序会依次在以下位置查找 FFmpeg：

1. 界面或 `--ffmpeg` 参数指定的位置
2. 系统 `PATH`
3. 程序目录、`bin` 子目录或 `ffmpeg/bin` 子目录

因此也可以把 `ffmpeg.exe` 和 `ffprobe.exe` 放到本项目的 `bin` 文件夹中，直接作为便携软件运行。

## 启动图形界面

双击 `start.bat`，或者运行：

```powershell
python video_splitter.py
```

操作顺序：添加视频或导入文件夹，选择输出目录，填写时长方案和命名模板，选择处理模式，然后点击“开始分割”。在文件选择窗口中可使用 `Ctrl` 或 `Shift` 同时选中多个视频。

“时长方案（分钟）”支持：

- 单一时长：`6`
- 小数分钟：`0.5`
- 多种时长：`3, 6, 10`

多时长模式会对每个源视频完整处理多次。例如输入 `3, 6, 10`，会分别生成3分钟、6分钟和10分钟三套片段。无损模式下，预计磁盘占用也约为源文件总大小的3倍；软件会在开始前显示确认信息。

## 批量调整分辨率

“输出分辨率”支持：

- 保持源视频尺寸：`原始`
- 单一目标尺寸：`1920x1080`、`1280x720`、`854x480`、`640x360`
- 多种尺寸：`原始, 1280x720, 640x360`

自定义宽和高必须是偶数。调整分辨率需要重新编码，确认后图形界面会使用精确模式输出 H.264/AAC MP4。缩放按源视频的显示比例计算，目标画幅多出的区域使用黑边填充。

时长和分辨率可以组合。例如时长填写 `3, 6`、分辨率填写 `1280x720, 640x360`，每个源视频会生成4套结果。软件会在开始前显示总任务数量。

## 批量自定义命名

视频列表新增“自定义名称”列。默认值是原文件名，可以通过两种方式修改：

- 双击某个视频的“自定义名称”单元格，单独修改名称
- 选中一个或多个视频后点击“批量命名…”，为选中项应用同一条规则；未选中时会处理列表中的全部视频

批量命名规则支持：

- `{name}`：原视频文件名
- `{video}`：待处理列表中的三位视频序号

例如规则 `实验A_{video}` 会依次生成自定义名称 `实验A_001`、`实验A_002`。自定义名称会同时用于输出目录和默认片段文件名。

## 统一命名模板

“片段命名模板”支持以下变量：

- `{name}`：原视频文件名，不含扩展名
- `{custom}`：列表中设置的自定义名称
- `{video}`：待处理列表中的视频序号，如 `001`
- `{duration}`：当前时长方案的分钟数，如 `2.5`
- `{resolution}`：当前输出分辨率，如 `1280x720` 或 `原始`
- `{index}`：当前视频的片段序号，如 `001`，此变量必须且只能出现一次

默认模板为 `{custom}_片段_{index}`。未修改自定义名称时，它与原文件名相同，因此源文件 `课程.mp4` 会输出 `课程_片段_001.mp4`。

如果希望所有视频使用完全统一的格式，可填写：

```text
课程_{video}_片段_{index}
```

批量处理时会生成 `课程_001_片段_001.mp4`、`课程_002_片段_001.mp4` 等文件。每个源视频仍保存在独立结果目录中，因此不会相互覆盖。

## 两种处理模式

### 快速无损（默认）

使用 FFmpeg 流复制，不重新编码；视频、音频和字幕流保持原样，速度通常远快于播放时长。软件会排除可能破坏分段封装的数据流和附件。由于只能安全地从关键帧开始，切点可能比设定时长略有偏差。

跨切点的字幕会按实际切点在前后片段中分别显示。遇到音频在画面结束后仍跨越分割切点的文件时，请使用精确时长模式。

开始前，软件会识别 AVI、Matroska、WebM 等容器并检查磁盘空间及编码兼容性。FFprobe 可用时，程序会逐段检查视频流和时长，并比较输出与源文件的主视频数据包数量。如果关键帧过稀导致未切开或出现覆盖多个目标时长的片段，程序会提示改用精确时长模式，已经生成的文件会保留。

### 精确时长

在每个设定时长边界强制关键帧并重新编码，使各完整片段精确到视频帧。为保证 Windows 播放兼容性，精确模式统一输出：

- 标准 MP4 容器
- H.264 High Profile 和 `avc1` 标签。H.264 等级由编码器根据画面尺寸和帧率等参数选择。
- AAC 音频
- 90 kHz 视频时间基
- `mp42` 品牌和前置 MP4 索引（Fast Start）

精确模式保留源帧率。选择“原始”时保留源画面，奇数宽或高会在右侧或底部补 1 像素黑边，以满足 H.264 的尺寸要求。选择目标尺寸时按设置缩放并补边。

当音频比视频画面明显更长时，精确模式会延长最后一帧画面来完成分割，并保留剩余音频。

## 命令行用法

精确切分：

```powershell
python video_splitter.py "D:\Videos\demo.mp4" -o "D:\Videos\output" --minutes 6 --mode exact
```

快速无损切分多个视频：

```powershell
python video_splitter.py "a.mp4" "b.mkv" -o output --mode copy
```

批量输出720p兼容MP4：

```powershell
python video_splitter.py "a.mp4" "b.mp4" -o output `
  --resolution "1280x720" --mode exact
```

同时输出720p和360p两套结果：

```powershell
python video_splitter.py "a.mp4" -o output `
  --resolutions "1280x720,640x360" --mode exact
```

为每个视频分别输出3分钟、6分钟和10分钟三套片段：

```powershell
python video_splitter.py "a.mp4" "b.mkv" -o output --durations "3,6,10" --mode copy
```

指定统一命名模板：

```powershell
python video_splitter.py "a.mp4" "b.mp4" -o output --name-template "课程_{video}_片段_{index}"
```

先按列表序号批量生成自定义名称，再用于片段名称：

```powershell
python video_splitter.py "a.mp4" "b.mp4" -o output `
  --custom-name-template "实验A_{video}" `
  --name-template "{custom}_{duration}分钟_{resolution}_{index}"
```

手动指定 FFmpeg：

```powershell
python video_splitter.py "demo.mp4" --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"
```

查看完整参数：

```powershell
python video_splitter.py --help
```

## 打包 Windows 可执行文件

项目提供 `VideoSplitter.spec` 和 `build_exe.bat`。双击构建脚本，或在 PowerShell 中运行：

```powershell
.\build_exe.bat
```

构建前请按照 [`bin/README.md`](bin/README.md) 将 `ffmpeg.exe` 和 `ffprobe.exe` 放入 `bin/`。GitHub 源代码仓库不会提交这些第三方二进制文件。

脚本会在项目内创建独立构建环境，并生成：

```text
dist\视频批量分割器.exe
```

该程序是无控制台窗口的单文件版本，已经内置 `ffmpeg.exe` 和 `ffprobe.exe`，目标电脑无需另外安装 Python 或 FFmpeg。由于首次启动时需要解压内置组件，启动速度可能比后续运行稍慢。

## 输出结构

单一时长保持原目录结构：

```text
output/
└── demo_片段/
    ├── demo_片段_001.mp4
    ├── demo_片段_002.mp4
    └── demo_片段_003.mp4
```

如果 `demo_片段` 已存在，程序会创建 `demo_片段_2`，不会覆盖上次结果。

新建的结果目录带有 `.video-splitter-output` 标记。导入上级文件夹时会跳过这些结果目录，即使重启后也一样。直接选择某个结果目录或具体片段时，仍可手动导入。

多时长模式使用时长标识区分目录：

```text
output/
├── demo_片段_3分钟/
├── demo_片段_6分钟/
└── demo_片段_10分钟/
```

## 开源许可与贡献

本项目以 [MIT License](LICENSE) 开源。FFmpeg 是独立的第三方项目，具体许可取决于所使用的构建版本，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

欢迎提交 Issue 和 Pull Request。开始前请阅读 [贡献指南](CONTRIBUTING.md)、[行为准则](CODE_OF_CONDUCT.md) 和 [安全政策](SECURITY.md)。
