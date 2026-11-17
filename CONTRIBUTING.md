# 贡献指南

感谢你改进视频分割转码工具。

## 开发环境

1. Fork 并克隆仓库。
2. 安装 Python 3.8 或更高版本。
3. 安装 FFmpeg，并确保 `ffmpeg`、`ffprobe` 位于 `PATH`，或放入 `bin/`。
4. 运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 提交修改

- 一个 Pull Request 聚焦一个主题。
- 新功能和错误修复应包含测试。
- 不要提交视频样本、构建目录、可执行文件或其他大型二进制文件。
- 保持界面文本、README 和命令行帮助同步。
- 提交前确保全部测试通过。

## 报告问题

请使用 Issue 模板，并提供：操作系统、Python/FFmpeg 版本、输入容器和编码信息、复现步骤，以及经过隐私处理的错误信息。请勿上传包含个人信息或未获授权的视频。
