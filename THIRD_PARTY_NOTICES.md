# 第三方组件说明

本项目通过命令行调用 [FFmpeg](https://ffmpeg.org/)，但 GitHub 源代码仓库不分发 FFmpeg 二进制文件。

FFmpeg 是独立项目，其许可证取决于具体构建选项，可能为 LGPL 2.1+ 或 GPL 2+。下载、构建或再分发 FFmpeg 时，请查阅所使用发行版附带的许可证和配置说明。

使用 PyInstaller 生成可执行文件时，也应遵守 [PyInstaller bootloader exception](https://pyinstaller.org/en/stable/license.html) 及所有被打包组件的许可证。
