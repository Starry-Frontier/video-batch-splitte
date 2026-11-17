# FFmpeg 目录

GitHub 仓库不提交 FFmpeg 二进制文件。

如需便携运行或构建 Windows 单文件程序，请从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html) 获取与你的平台和许可证需求相符的发行版，并将以下文件放入本目录：

```text
bin/
├── ffmpeg.exe
└── ffprobe.exe
```

也可以把 FFmpeg 安装到系统 `PATH`；直接运行 Python 程序时无需复制到此目录。打包脚本要求上述两个文件存在。
