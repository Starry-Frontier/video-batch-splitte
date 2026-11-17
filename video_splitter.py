#!/usr/bin/env python3
"""将视频批量切分为一种或多种固定时长片段的桌面工具和命令行程序。"""

import argparse
import csv
import json
import math
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple


APP_NAME = "视频分割转码"
OUTPUT_MARKER = ".video-splitter-output"
DEFAULT_MINUTES = 6.0
DEFAULT_CUSTOM_NAME_TEMPLATE = "{name}"
DEFAULT_NAME_TEMPLATE = "{custom}_片段_{index}"
CUSTOM_NAME_PATTERN_FIELDS = ("name", "video")
NAME_TEMPLATE_FIELDS = (
    "name",
    "custom",
    "video",
    "duration",
    "resolution",
    "index",
)
VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".vob",
    ".ts",
    ".mts",
    ".m2ts",
    ".3gp",
    ".ogv",
    ".ogg",
)
CONTAINER_SUFFIXES = {
    "avi": ".avi",
    "asf": ".wmv",
    "flv": ".flv",
    "matroska": ".mkv",
    "webm": ".webm",
    "mpeg": ".mpg",
    "mpegts": ".ts",
    "ogg": ".ogv",
}


class VideoSplitError(RuntimeError):
    """视频分割失败。"""


class FFmpegNotFoundError(VideoSplitError):
    """找不到 FFmpeg。"""


class BatchSettings(NamedTuple):
    inputs: List[Path]
    custom_names: List[str]
    output_root: Path
    durations: List[float]
    resolutions: List[Optional[Tuple[int, int]]]
    mode: str
    name_template: str
    ffmpeg_path: str
    ffprobe_path: Optional[str]


def format_minutes_value(minutes: float) -> str:
    """返回适合界面、文件名和目录名的分钟数文本。"""
    if not math.isfinite(minutes) or minutes <= 0:
        raise VideoSplitError("片段时长必须是大于 0 的有限数字。")
    return ("{:.6f}".format(minutes)).rstrip("0").rstrip(".")


def parse_duration_list(value: str) -> List[float]:
    """解析以逗号、分号或空白分隔的分钟数，并按输入顺序去重。"""
    tokens = [item for item in re.split(r"[,，;；\s]+", value.strip()) if item]
    if not tokens:
        raise VideoSplitError("请至少输入一个片段时长。")

    durations = []  # type: List[float]
    seen = set()
    for token in tokens:
        try:
            minutes = float(token)
        except ValueError:
            raise VideoSplitError("无法识别时长“{}”；请输入分钟数。".format(token))
        if not math.isfinite(minutes) or minutes <= 0:
            raise VideoSplitError("片段时长必须是大于 0 的有限数字：{}".format(token))
        normalized = round(minutes, 6)
        if normalized <= 0:
            raise VideoSplitError("片段时长精度最多支持 6 位小数：{}".format(token))
        if normalized not in seen:
            durations.append(normalized)
            seen.add(normalized)
    return durations


def parse_resolution_list(value: str) -> List[Optional[Tuple[int, int]]]:
    """解析原始分辨率或一个/多个“宽x高”分辨率，并按输入顺序去重。"""
    tokens = [item.strip() for item in re.split(r"[,，;；]+", value.strip()) if item.strip()]
    if not tokens:
        raise VideoSplitError("请至少输入一个输出分辨率。")

    resolutions = []  # type: List[Optional[Tuple[int, int]]]
    seen = set()
    original_aliases = {"原始", "保持原始", "原分辨率", "original", "source"}
    for token in tokens:
        if token.lower() in original_aliases:
            resolution = None  # type: Optional[Tuple[int, int]]
        else:
            match = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", token)
            if not match:
                raise VideoSplitError(
                    "无法识别分辨率“{}”；请使用“原始”或“宽x高”，例如 1280x720。".format(
                        token
                    )
                )
            width, height = (int(match.group(1)), int(match.group(2)))
            if width < 2 or height < 2 or width > 16384 or height > 16384:
                raise VideoSplitError("分辨率必须在 2x2 到 16384x16384 之间：{}".format(token))
            if width % 2 or height % 2:
                raise VideoSplitError(
                    "H.264 兼容输出要求宽和高均为偶数：{}x{}".format(width, height)
                )
            resolution = (width, height)
        if resolution not in seen:
            resolutions.append(resolution)
            seen.add(resolution)
    return resolutions


def format_resolution_label(resolution: Optional[Tuple[int, int]]) -> str:
    """返回用于界面、文件名和目录名的分辨率文本。"""
    if resolution is None:
        return "原始"
    return "{}x{}".format(resolution[0], resolution[1])


def output_group_label(
    minutes: float,
    duration_count: int,
    resolution: Optional[Tuple[int, int]],
    resolution_count: int,
) -> Optional[str]:
    """为多时长/多分辨率输出构造清晰且稳定的目录标签。"""
    labels = []  # type: List[str]
    if duration_count > 1:
        labels.append(format_minutes_value(minutes) + "分钟")
    if resolution_count > 1 or resolution is not None:
        labels.append(format_resolution_label(resolution))
    return "_".join(labels) or None


def _program_candidates(name: str) -> Iterable[Path]:
    executable = name + (".exe" if os.name == "nt" else "")
    app_dir = Path(__file__).resolve().parent
    yield app_dir / executable
    yield app_dir / "bin" / executable
    yield app_dir / "ffmpeg" / "bin" / executable


def find_program(name: str, explicit: Optional[str] = None) -> Optional[str]:
    """查找 FFmpeg/FFprobe，优先使用用户明确指定的位置。"""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            path = path / (name + (".exe" if os.name == "nt" else ""))
        if path.is_file():
            return str(path.resolve())
        return None

    environment_name = "FFMPEG_BINARY" if name == "ffmpeg" else "FFPROBE_BINARY"
    environment_value = os.environ.get(environment_name)
    if environment_value:
        path = Path(environment_value).expanduser()
        if path.is_file():
            return str(path.resolve())

    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    for candidate in _program_candidates(name):
        if candidate.is_file():
            return str(candidate)
    return None


def companion_ffprobe(ffmpeg_path: str) -> Optional[str]:
    """在 ffmpeg 同目录中查找 ffprobe，然后回退到常规搜索。"""
    executable = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    beside_ffmpeg = Path(ffmpeg_path).resolve().parent / executable
    if beside_ffmpeg.is_file():
        return str(beside_ffmpeg)
    return find_program("ffprobe")


def probe_media_info(
    input_path: Path,
    ffprobe_path: Optional[str],
    *,
    count_video_packets: bool = False,
    runner: Optional["FFmpegRunner"] = None,
) -> Optional[Dict[str, Any]]:
    """返回容器、时长及流编码信息；无法读取时返回 None。"""
    if not ffprobe_path:
        return None
    command = [ffprobe_path, "-v", "error"]
    if count_video_packets:
        command.append("-count_packets")
    command.extend([
        "-show_entries",
        "format=format_name,start_time,duration,size,bit_rate:"
        "stream=index,codec_type,codec_name,codec_tag_string,profile,pix_fmt,"
        "width,height,time_base,start_time,duration,level,r_frame_rate,nb_read_packets:"
        "stream_disposition=attached_pic,default:stream_tags=DURATION:format_tags=major_brand",
        "-of",
        "json",
        str(input_path),
    ])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # 数数据包需要顺序读完整个文件，使用可取消的进程，不套用快速探测的超时。
        timeout = None if count_video_packets else 30
        if runner:
            with runner.process(command, subprocess.PIPE) as process:
                output, _errors = process.communicate(timeout=timeout)
                runner.check_cancelled()
                if process.returncode != 0:
                    return None
        else:
            result = subprocess.run(
                command,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                return None
            output = result.stdout
        value = json.loads(output)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def probe_duration(input_path: Path, ffprobe_path: Optional[str]) -> Optional[float]:
    """返回媒体时长（秒）；无法读取时返回 None。"""
    info = probe_media_info(input_path, ffprobe_path)
    if not info:
        return None
    try:
        value = info.get("format", {}).get("duration")
        duration = float(value)
        return duration if duration > 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def _streams_of_type(info: Dict[str, Any], codec_type: str) -> List[Dict[str, Any]]:
    streams = info.get("streams", [])
    if not isinstance(streams, list):
        return []
    return [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type
    ]


def _main_video(info: Dict[str, Any]) -> Dict[str, Any]:
    videos = [
        stream for stream in _streams_of_type(info, "video")
        if not stream.get("disposition", {}).get("attached_pic")
    ]
    return next(
        (stream for stream in videos if stream.get("disposition", {}).get("default")),
        videos[0] if videos else {},
    )


def _seconds(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _stream_end(stream: Dict[str, Any], origin: float) -> Optional[float]:
    duration = _seconds(stream.get("duration"))
    if duration is not None and duration > 0:
        start = _seconds(stream.get("start_time"))
        return duration + (origin if start is None else start)
    stamp = stream.get("tags", {}).get("DURATION", "")
    if stamp:
        return _parse_progress_seconds("out_time=" + stamp)
    return None


def _frame_seconds(video: Dict[str, Any]) -> float:
    rate = video.get("r_frame_rate", "")
    try:
        numerator, denominator = rate.split("/")
        seconds = int(denominator) / int(numerator)
        return seconds if seconds > 0 else 0.04
    except (AttributeError, ValueError, ZeroDivisionError):
        return 0.04


def _probe_packet_ends(
    input_path: Path,
    ffprobe_path: str,
    fallback_durations: Dict[int, float],
    runner: "FFmpegRunner",
) -> Dict[int, float]:
    """时长元数据缺失时，逐行读取实际的包结束时间。"""
    command = [
        ffprobe_path, "-v", "error", "-show_entries",
        "packet=stream_index,pts_time,dts_time,duration_time",
        "-of", "compact=p=0", str(input_path),
    ]
    ends = {}  # type: Dict[int, float]
    with runner.process(command, subprocess.DEVNULL) as process:
        if process.stdout:
            for line in process.stdout:
                index_field, _, timing_fields = line.partition("|")
                index_text = index_field.strip().partition("=")[2]
                if not index_text.isdecimal():
                    continue
                index = int(index_text)
                if index not in fallback_durations:
                    continue
                timing = dict(
                    field.split("=", 1) for field in timing_fields.strip().split("|") if "=" in field
                )
                start = _seconds(timing.get("pts_time"))
                if start is None:
                    start = _seconds(timing.get("dts_time"))
                if start is not None:
                    duration = _seconds(timing.get("duration_time")) or fallback_durations[index]
                    end = start + max(0.0, duration)
                    ends[index] = max(ends.get(index, end), end)
        status = process.wait()
        runner.check_cancelled()
        return ends if status == 0 else {}


def _playback_bounds(
    info: Dict[str, Any],
    input_path: Path,
    ffprobe_path: Optional[str],
    runner: "FFmpegRunner",
) -> Tuple[float, Optional[float], Optional[float]]:
    origin = _seconds(info.get("format", {}).get("start_time")) or 0.0
    video = _main_video(info)
    audio = _streams_of_type(info, "audio")
    video_end = _stream_end(video, origin)
    audio_ends = [_stream_end(stream, origin) for stream in audio]
    if ffprobe_path and video and audio:
        missing = {
            stream["index"]: 0.0
            for stream, end in zip(audio, audio_ends) if end is None and "index" in stream
        }
        if video_end is None and "index" in video:
            missing[video["index"]] = _frame_seconds(video)
        if missing:
            packet_ends = _probe_packet_ends(input_path, ffprobe_path, missing, runner)
            if video_end is None:
                video_end = packet_ends.get(video.get("index"))
            audio_ends = [
                packet_ends.get(stream.get("index")) if end is None else end
                for stream, end in zip(audio, audio_ends)
            ]
    audio_end = max((end for end in audio_ends if end is not None), default=None)
    return origin, video_end, audio_end


def _video_packet_count(video: Dict[str, Any]) -> int:
    value = str(video.get("nb_read_packets", ""))
    return int(value) if value.isdecimal() else 0


def _ebml_document_type(input_path: Path) -> Optional[bytes]:
    """从文件开头的 EBML 头读取 DocType，区分共用解复用器的两个容器。"""
    try:
        with input_path.open("rb") as source:
            header = source.read(4096)
    except OSError:
        return None
    if not header.startswith(b"\x1a\x45\xdf\xa3"):
        return None

    def read_vint(offset: int, keep_marker: bool = False) -> Tuple[int, int]:
        first = header[offset]
        width = 9 - first.bit_length()
        end = offset + width
        if width > 8 or end > len(header):
            raise ValueError
        value = int.from_bytes(header[offset:end], "big")
        if not keep_marker:
            value ^= 1 << (7 * width)
        return value, end

    try:
        size, cursor = read_vint(4)
        end = min(cursor + size, len(header))
        while cursor < end:
            element, cursor = read_vint(cursor, keep_marker=True)
            size, cursor = read_vint(cursor)
            if cursor + size > end:
                break
            if element == 0x4282:
                return header[cursor:cursor + size]
            cursor += size
    except (IndexError, ValueError):
        pass
    return None


def detected_container_suffix(
    input_path: Path,
    info: Optional[Dict[str, Any]],
) -> str:
    """根据媒体内容返回真实容器扩展名，而不是盲目信任文件名。"""
    fallback = input_path.suffix.lower() or ".mp4"
    if not info:
        return fallback
    format_info = info.get("format", {})
    if not isinstance(format_info, dict):
        return fallback
    format_names = {
        item.strip().lower()
        for item in str(format_info.get("format_name", "")).split(",")
        if item.strip()
    }
    if "avi" in format_names:
        return ".avi"
    if {"matroska", "webm"} <= format_names:
        document_type = _ebml_document_type(input_path)
        if document_type == b"webm":
            return ".webm"
        if document_type == b"matroska":
            return ".mkv"
        return fallback if fallback in (".webm", ".mkv") else ".mkv"
    if format_names.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}):
        brand = str(format_info.get("tags", {}).get("major_brand", "")).strip().lower()
        if brand == "qt":
            return ".mov"
        if brand.startswith("3gp"):
            return ".3gp"
        if brand:
            return fallback if fallback in (".mp4", ".m4v") else ".mp4"
        return fallback if fallback in (".mp4", ".m4v", ".mov", ".3gp") else ".mp4"
    for format_name, suffix in CONTAINER_SUFFIXES.items():
        if format_name in format_names:
            return suffix
    return fallback


def compatibility_warnings(
    input_path: Path,
    mode: str,
    ffprobe_path: Optional[str],
    *,
    runner: Optional["FFmpegRunner"] = None,
) -> List[str]:
    """检查输入编码与所选模式，返回处理前应展示的兼容性提醒。"""
    if mode not in ("copy", "exact"):
        raise VideoSplitError("未知处理模式：{}".format(mode))

    info = probe_media_info(input_path, ffprobe_path, runner=runner)
    if not info:
        return [
            "{}：无法读取编码信息；无法预先确认{}兼容性。".format(
                input_path.name,
                "无损输出" if mode == "copy" else "精确 MP4 输出",
            )
        ]

    warnings = []  # type: List[str]
    actual_suffix = detected_container_suffix(input_path, info)
    named_suffix = input_path.suffix.lower()
    if actual_suffix != named_suffix:
        if mode == "copy":
            warnings.append(
                "{}：扩展名是 {}，但实际容器是 {}；无损输出将自动改用 {}。".format(
                    input_path.name,
                    named_suffix or "无",
                    actual_suffix.lstrip(".").upper(),
                    actual_suffix,
                )
            )
        else:
            warnings.append(
                "{}：扩展名与真实容器不一致；精确模式将输出标准兼容 MP4。".format(
                    input_path.name
                )
            )

    if mode == "exact":
        return warnings

    video = _main_video(info)
    audio_streams = _streams_of_type(info, "audio")
    video_codec = str(video.get("codec_name", "")).lower()
    video_tag = str(video.get("codec_tag_string", "")).lower()
    audio_codec = str(audio_streams[0].get("codec_name", "")).lower() if audio_streams else ""
    audio_tag = str(audio_streams[0].get("codec_tag_string", "")).lower() if audio_streams else ""

    if video_codec == "mpeg4" or video_tag in ("mp4v", "fmp4", "xvid", "divx"):
        warnings.append(
            "{}：源视频为 MPEG-4 Part 2（{}）；无损模式会原样保留，Windows 自带播放器可能不支持。".format(
                input_path.name,
                video_tag or video_codec,
            )
        )
    if audio_codec == "mp3" or audio_tag in ("ip3m", ".mp3"):
        warnings.append(
            "{}：源音频为 MP3；无损模式会原样保留，部分容器在 Windows 自带播放器中可能不兼容。".format(
                input_path.name
            )
        )
    return warnings


def verify_split_outputs(
    input_path: Path,
    files: Sequence[Path],
    mode: str,
    ffprobe_path: Optional[str],
    expected_resolution: Optional[Tuple[int, int]] = None,
    *,
    segment_seconds: Optional[float] = None,
    source_info: Optional[Dict[str, Any]] = None,
    runner: Optional["FFmpegRunner"] = None,
) -> None:
    """验证输出可解析性，并检查无损模式是否出现异常数据缺失。"""
    if not files:
        raise VideoSplitError("FFmpeg 已结束，但没有生成任何片段。")

    video_packet_total = 0
    longest_video = 0.0
    frame_seconds = 0.0
    for file_path in files:
        if runner:
            runner.check_cancelled()
        if not file_path.is_file() or file_path.stat().st_size == 0:
            raise VideoSplitError("输出完整性检查失败：{} 是空文件。".format(file_path.name))
        if not ffprobe_path:
            continue
        info = probe_media_info(
            file_path, ffprobe_path, count_video_packets=mode == "copy", runner=runner,
        )
        if not info:
            raise VideoSplitError("输出兼容性检查失败：{} 无法被 FFprobe 识别。".format(file_path.name))
        video = _main_video(info)
        if not video:
            raise VideoSplitError("输出兼容性检查失败：{} 不包含视频流。".format(file_path.name))
        try:
            duration = float(info.get("format", {}).get("duration"))
        except (ValueError, TypeError, AttributeError):
            duration = 0.0
        if not math.isfinite(duration) or duration <= 0:
            raise VideoSplitError("输出兼容性检查失败：{} 时长无效。".format(file_path.name))

        if mode == "copy":
            video_packet_total += _video_packet_count(video)
            try:
                video_seconds = float(video["duration"])
            except (KeyError, TypeError, ValueError):
                timestamp = video.get("tags", {}).get("DURATION", "")
                video_seconds = _parse_progress_seconds("out_time=" + timestamp) or duration
            longest_video = max(longest_video, video_seconds)
            try:
                numerator, denominator = video["r_frame_rate"].split("/")
                frame_seconds = max(frame_seconds, int(denominator) / int(numerator))
            except (KeyError, AttributeError, ValueError, ZeroDivisionError):
                pass

        if mode == "exact":
            audio_streams = _streams_of_type(info, "audio")
            streams = [video] + audio_streams
            codecs = {str(stream.get("codec_name", "")).lower() for stream in streams}
            tags = {str(stream.get("codec_tag_string", "")).lower() for stream in streams}
            if "mpeg4" in codecs or "mp3" in codecs or "mp4v" in tags or "ip3m" in tags:
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 仍包含已禁用的 mp4v/MP3 编码。".format(
                        file_path.name
                    )
                )
            format_info = info.get("format", {})
            format_names = str(format_info.get("format_name", "")) if isinstance(format_info, dict) else ""
            if "mp4" not in format_names and "mov" not in format_names:
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 不是标准 MP4 容器。".format(file_path.name)
                )
            if str(video.get("codec_name", "")).lower() != "h264":
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 的视频不是 H.264。".format(file_path.name)
                )
            if str(video.get("codec_tag_string", "")).lower() != "avc1":
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 的 H.264 标签不是 avc1。".format(file_path.name)
                )
            if any(str(stream.get("codec_name", "")).lower() != "aac" for stream in audio_streams):
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 包含非 AAC 音频。".format(file_path.name)
                )
            if str(video.get("time_base", "")) != "1/90000":
                raise VideoSplitError(
                    "输出兼容性检查失败：{} 未使用 Windows 兼容的 90 kHz 视频时间基。".format(
                        file_path.name
                    )
                )
            if expected_resolution is not None:
                actual_resolution = (
                    video.get("width"),
                    video.get("height"),
                )
                if actual_resolution != expected_resolution:
                    raise VideoSplitError(
                        "输出分辨率检查失败：{} 实际为 {}x{}，期望为 {}x{}。".format(
                            file_path.name,
                            actual_resolution[0],
                            actual_resolution[1],
                            expected_resolution[0],
                            expected_resolution[1],
                        )
                    )

    if mode == "copy" and ffprobe_path:
        if source_info is None:
            source_info = probe_media_info(
                input_path, ffprobe_path, count_video_packets=True, runner=runner,
            )
        source_packets = _video_packet_count(_main_video(source_info or {}))
        if source_packets and video_packet_total < source_packets:
            raise VideoSplitError(
                "无损完整性检查失败：主视频输出包含 {} 个数据包，源文件有 {} 个。"
                "已保留输出供检查。".format(video_packet_total, source_packets)
            )
        if segment_seconds is not None:
            # 无损模式允许正常关键帧偏差，但一段覆盖两个目标时长说明切点被跳过。
            limit = segment_seconds * (1 if len(files) == 1 else 2)
            if longest_video > limit + max(0.1, frame_seconds * 2):
                raise VideoSplitError(
                    "源视频关键帧间隔过大，未能按 {:.2f} 秒分割（最长片段 {:.2f} 秒）。"
                    "已保留输出，可改用精确时长模式。".format(segment_seconds, longest_video)
                )


def safe_stem(path: Path) -> str:
    """生成适合 FFmpeg 序列文件名的名称。"""
    # 百分号会被 FFmpeg 当作序号格式符，其余字符已经是合法的源文件名字符。
    stem = path.stem.replace("%", "_").strip(" .")
    return stem or "video"


def validate_custom_name(value: str) -> str:
    """验证单个源视频的自定义输出名称。"""
    value = value.strip(" .")
    if not value:
        raise VideoSplitError("自定义名称不能为空。")
    if "%" in value:
        raise VideoSplitError("自定义名称不能包含百分号（%）。")
    invalid = '<>:"/\\|?*'
    if any(character in value for character in invalid):
        raise VideoSplitError("自定义名称不能包含以下文件名字符：{}".format(invalid))
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if value.split(".", 1)[0].upper() in reserved:
        raise VideoSplitError("自定义名称不能使用 Windows 保留名称：{}".format(value))
    return value


def render_custom_name_pattern(
    input_path: Path,
    pattern: str = DEFAULT_CUSTOM_NAME_TEMPLATE,
    video_index: int = 1,
) -> str:
    """用原文件名和视频序号批量生成自定义名称。"""
    pattern = pattern.strip()
    if not pattern:
        raise VideoSplitError("批量命名规则不能为空。")
    if "%" in pattern:
        raise VideoSplitError("批量命名规则不能包含百分号（%）。")
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(pattern):
            if field_name is None:
                continue
            if field_name not in CUSTOM_NAME_PATTERN_FIELDS:
                raise VideoSplitError(
                    "批量命名规则包含未知变量 {{{}}}；仅支持 {}。".format(
                        field_name,
                        "、".join(
                            "{{{}}}".format(item) for item in CUSTOM_NAME_PATTERN_FIELDS
                        ),
                    )
                )
            if format_spec or conversion:
                raise VideoSplitError("批量命名变量不支持格式说明符或转换符。")
    except ValueError as exc:
        raise VideoSplitError("批量命名规则中的大括号不匹配：{}".format(exc))

    rendered = pattern.format(
        name=safe_stem(input_path),
        video="{:03d}".format(max(1, video_index)),
    )
    return validate_custom_name(rendered)


def video_filetypes() -> List[Tuple[str, object]]:
    """返回 Tk 原生支持的多扩展名文件过滤器。"""
    patterns = tuple("*" + suffix for suffix in VIDEO_EXTENSIONS)
    return [
        ("所有支持的视频", patterns),
        ("MP4 视频", "*.mp4"),
        ("MKV 视频", "*.mkv"),
        ("MOV 视频", "*.mov"),
        ("AVI 视频", "*.avi"),
        ("所有文件", "*.*"),
    ]


def collect_video_files(
    folder: Path, recursive: bool = True, exclude_folders: Sequence[Path] = (),
) -> List[Path]:
    """从文件夹中收集支持的视频文件。"""
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        return []
    excluded = {path.resolve() for path in exclude_folders}
    files = []
    for directory, subfolders, filenames in os.walk(folder):
        directory = Path(directory)
        videos = [
            directory / name for name in filenames
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS and (directory / name).is_file()
        ]
        files.extend(videos)
        if not recursive:
            break
        source_stems = {safe_stem(path).casefold() for path in videos}
        subfolders[:] = [
            name for name in subfolders
            if directory / name not in excluded
            and not (directory / name / OUTPUT_MARKER).is_file()
            and not _is_legacy_output_folder(directory / name, source_stems)
        ]
    return sorted(files, key=lambda item: str(item).lower())


def _is_legacy_output_folder(folder: Path, source_stems: Set[str]) -> bool:
    """识别旧版默认命名的输出，同时要求同级有对应的源视频。"""
    stem, separator, suffix = folder.name.casefold().rpartition("_片段")
    if not separator or stem not in source_stems:
        return False
    if not re.fullmatch(r"(?:_[0-9]+(?:\.[0-9]+)?分钟)?(?:_原始|_[0-9]+x[0-9]+)?(?:_[0-9]+)?", suffix):
        return False
    try:
        clips = [
            path.stem.casefold() for path in folder.iterdir()
            if path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file()
        ]
    except OSError:
        return False
    prefix = stem + separator
    clip_pattern = re.escape(prefix) + r"_[0-9]{3,}"
    return prefix + "_001" in clips and all(re.fullmatch(clip_pattern, clip) for clip in clips)


def validate_name_template(template: str) -> str:
    """验证片段命名模板并返回去除首尾空白后的模板。"""
    template = template.strip()
    if not template:
        raise VideoSplitError("命名模板不能为空。")
    if "%" in template:
        raise VideoSplitError("命名模板不能包含百分号（%）。")

    fields = []  # type: List[str]
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
            if field_name is None:
                continue
            if field_name not in NAME_TEMPLATE_FIELDS:
                raise VideoSplitError(
                    "命名模板包含未知变量 {{{}}}；仅支持 {}。".format(
                        field_name,
                        "、".join("{{{}}}".format(item) for item in NAME_TEMPLATE_FIELDS),
                    )
                )
            if format_spec or conversion:
                raise VideoSplitError("命名变量不支持格式说明符或转换符。")
            fields.append(field_name)
    except ValueError as exc:
        raise VideoSplitError("命名模板中的大括号不匹配：{}".format(exc))

    if fields.count("index") != 1:
        raise VideoSplitError("命名模板必须包含且只能包含一个 {index}，用于区分片段。")
    return template


def render_output_name(
    input_path: Path,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    video_index: int = 1,
    duration_minutes: float = DEFAULT_MINUTES,
    custom_name: Optional[str] = None,
    resolution: Optional[Tuple[int, int]] = None,
) -> str:
    """把用户模板转换为 FFmpeg 可用的序列文件名。"""
    template = validate_name_template(name_template)
    rendered = template.format(
        name=safe_stem(input_path),
        custom=validate_custom_name(custom_name or safe_stem(input_path)),
        video="{:03d}".format(max(1, video_index)),
        duration=format_minutes_value(duration_minutes),
        resolution=format_resolution_label(resolution),
        index="%03d",
    ).strip(" .")
    if not rendered:
        raise VideoSplitError("命名模板生成了空文件名。")
    invalid = '<>:"/\\|?*'
    if any(character in rendered for character in invalid):
        raise VideoSplitError("命名模板不能包含以下文件名字符：{}".format(invalid))
    return rendered


def create_unique_output_folder(
    output_root: Path,
    input_path: Path,
    group_label: Optional[str] = None,
    custom_name: Optional[str] = None,
) -> Path:
    """创建不覆盖既有结果的输出目录。"""
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = validate_custom_name(custom_name or safe_stem(input_path)) + "_片段"
    if group_label:
        invalid = '<>:"/\\|?*'
        cleaned_label = "".join("_" if character in invalid else character for character in group_label)
        cleaned_label = cleaned_label.strip(" ._")
        if not cleaned_label:
            raise VideoSplitError("输出分组名称无效。")
        base_name += "_" + cleaned_label
    base = output_root / base_name
    candidate = base
    index = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = output_root / (base.name + "_" + str(index))
            index += 1


def output_pattern(
    output_folder: Path,
    input_path: Path,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    video_index: int = 1,
    output_suffix: Optional[str] = None,
    duration_minutes: float = DEFAULT_MINUTES,
    custom_name: Optional[str] = None,
    resolution: Optional[Tuple[int, int]] = None,
) -> Path:
    """创建从 001 开始编号、使用指定真实容器扩展名的输出模板。"""
    suffix = (output_suffix or input_path.suffix or ".mp4").lower()
    if not suffix.startswith("."):
        suffix = "." + suffix
    return output_folder / (
        render_output_name(
            input_path,
            name_template,
            video_index,
            duration_minutes,
            custom_name,
            resolution,
        )
        + suffix
    )


def _exact_codec_arguments(suffix: str) -> List[str]:
    """精确模式统一输出 Windows 兼容的 H.264/AAC MP4。"""
    suffix = suffix.lower()
    if suffix != ".mp4":
        raise VideoSplitError("精确模式必须输出标准 MP4，当前输出扩展名为 {}。".format(suffix))
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
    ]


def build_ffmpeg_command(
    ffmpeg_path: str,
    input_path: Path,
    pattern: Path,
    segment_seconds: float,
    mode: str,
    resolution: Optional[Tuple[int, int]] = None,
    *,
    video_stream_index: Optional[int] = None,
    video_tail: float = 0.0,
    segment_list: Optional[Path] = None,
) -> List[str]:
    """构造分割命令。mode 可取 exact 或 copy。"""
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise VideoSplitError("片段时长必须是大于 0 的有限数字")
    if mode not in ("exact", "copy"):
        raise ValueError("未知处理模式：{}".format(mode))
    if resolution is not None:
        width, height = resolution
        if (
            width < 2
            or height < 2
            or width > 16384
            or height > 16384
            or width % 2
            or height % 2
        ):
            raise ValueError("输出分辨率必须是大于 0 的偶数尺寸")
        if mode == "copy":
            raise ValueError("快速无损模式不能调整分辨率；请使用精确时长模式")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
    ]
    video_map = "0:V:0" if video_stream_index is None else "0:{}".format(video_stream_index)

    if mode == "copy":
        # 只复制播放所需的主视频、音频和字幕；排除会导致分段封装失败的数据流与附件。
        command.extend(
            [
                "-map",
                video_map,
                "-map",
                "0:a?",
                "-map_metadata",
                "0",
                "-dn",
                "-c",
                "copy",
            ]
        )
        if segment_list is None:
            command.extend(["-map", "0:s?"])
    else:
        # 精确模式只处理主视频和所有音轨，避免封面图或不兼容字幕破坏容器。
        command.extend(["-map", video_map, "-map", "0:a?", "-map_metadata", "0"])
        command.extend(_exact_codec_arguments(pattern.suffix))
        if resolution is not None:
            width, height = resolution
            # dar 包含像素宽高比。先按显示比例缩放成方形像素，再补边。
            video_filter = (
                "scale=w='max(2,2*round(min({w},{h}*dar)/2))':"
                "h='max(2,2*round(min({h},{w}/dar)/2))',setsar=1,"
                "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
            ).format(w=width, h=height)
        else:
            video_filter = (
                "pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:color=black"
            )
        if video_tail > 0:
            video_filter += ",tpad=stop_mode=clone:stop_duration={:.6f}".format(video_tail)
        command.extend(["-vf", video_filter])
        command.extend(
            [
                "-force_key_frames",
                "expr:gte(t,n_forced*{:.6f})".format(segment_seconds),
            ]
        )

    command.extend(
        [
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "segment",
            "-segment_time",
            "{:.6f}".format(segment_seconds),
            "-segment_start_number",
            "1",
            "-reset_timestamps",
            "1",
        ]
    )
    if segment_list is not None:
        # 清单使用源时间轴，供第二次封装字幕时按实际视频切点裁剪。
        command.extend([
            "-segment_list_type", "csv", "-segment_list", str(segment_list),
            "-avoid_negative_ts", "disabled",
        ])
    else:
        command.extend(["-avoid_negative_ts", "make_zero"])
    # 分段后音频包可能比视频切点早。每个子容器独立平移时间戳，以保留这些包。
    segment_options = "avoid_negative_ts=make_non_negative"
    if mode == "exact":
        command.extend(
            [
                "-segment_format",
                "mp4",
            ]
        )
        segment_options += ":movflags=+faststart:video_track_timescale=90000:brand=mp42"
    command.extend(["-segment_format_options", segment_options])
    parent = str(pattern.parent).replace("%", "%%")
    command.append(str(Path(parent) / pattern.name))
    return command


def _parse_progress_seconds(line: str) -> Optional[float]:
    key, separator, value = line.partition("=")
    if not separator:
        return None
    if key in ("out_time_us", "out_time_ms"):
        try:
            # out_time_ms 是 FFmpeg 为兼容历史保留的名字，实际单位同样是微秒。
            return max(0.0, float(value) / 1_000_000.0)
        except ValueError:
            return None
    if key == "out_time":
        try:
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (ValueError, TypeError):
            return None
    return None


class FFmpegRunner:
    """执行 FFmpeg，并允许来自 GUI 线程的安全取消。"""

    def __init__(self) -> None:
        self._process = None  # type: Optional[subprocess.Popen]
        self._lock = threading.Lock()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise VideoSplitError("处理已取消")

    @contextmanager
    def process(self, command: Sequence[str], stderr: int) -> Iterator[subprocess.Popen]:
        self.check_cancelled()
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._process = process
        try:
            with process:
                try:
                    self.check_cancelled()
                    yield process
                finally:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            pass
        finally:
            with self._lock:
                self._process = None

    def run(
        self,
        command: Sequence[str],
        duration: Optional[float],
        progress_callback: Optional[Callable[[Optional[float]], None]] = None,
    ) -> None:
        messages = []  # type: List[str]
        try:
            with self.process(command, subprocess.STDOUT) as process:
                if process.stdout:
                    for raw_line in process.stdout:
                        line = raw_line.strip()
                        seconds = _parse_progress_seconds(line)
                        if seconds is not None and progress_callback:
                            progress_callback(min(1.0, seconds / duration) if duration else None)
                        elif line and "=" not in line:
                            messages.append(line)
                            messages = messages[-20:]
                return_code = process.wait()
        except OSError as exc:
            self.check_cancelled()
            raise VideoSplitError("FFmpeg 运行失败：{}".format(exc)) from exc

        self.check_cancelled()
        if return_code != 0:
            detail = "\n".join(messages[-8:]) or "FFmpeg 未返回详细错误信息"
            raise VideoSplitError("FFmpeg 处理失败：\n{}".format(detail))
        if progress_callback:
            progress_callback(1.0)


def ensure_copy_disk_space(input_path: Path, output_root: Path) -> None:
    """无损模式开始前确认目标磁盘可容纳接近源文件大小的数据。"""
    existing = output_root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free_bytes = shutil.disk_usage(str(existing)).free
    except OSError:
        return
    required_bytes = int(input_path.stat().st_size * 1.03) + 64 * 1024 * 1024
    if free_bytes < required_bytes:
        raise VideoSplitError(
            "目标磁盘空间不足：无损分割至少需要 {:.1f} GB，当前可用 {:.1f} GB。".format(
                required_bytes / (1024 ** 3),
                free_bytes / (1024 ** 3),
            )
        )


def cleanup_empty_failed_outputs(folder: Path) -> None:
    """清理本次失败产生的0字节文件；不删除任何含数据的片段。"""
    try:
        marker = folder / OUTPUT_MARKER
        for item in folder.iterdir():
            if item != marker and item.is_file() and item.stat().st_size == 0:
                item.unlink()
        if folder.is_dir() and not any(item != marker for item in folder.iterdir()):
            marker.unlink(missing_ok=True)
            folder.rmdir()
    except OSError:
        # 清理失败不应覆盖真正的 FFmpeg 错误。
        pass


def _restore_segment_subtitles(
    ffmpeg_path: str,
    ffprobe_path: str,
    input_path: Path,
    folder: Path,
    listing: Path,
    output_suffix: str,
    bounds: Tuple[float, Optional[float], Optional[float]],
    source_video_start: Optional[float],
    runner: FFmpegRunner,
) -> None:
    try:
        with listing.open(encoding="utf-8", newline="") as stream:
            segments = [(name, float(start), float(end)) for name, start, end in csv.reader(stream)]
    except (OSError, ValueError, csv.Error) as exc:
        raise VideoSplitError("无法读取分段切点，字幕未封装。") from exc
    if not segments:
        raise VideoSplitError("未生成用于字幕封装的分段切点。")

    origin, video_end, audio_end = bounds
    playback_end = max(end for end in (origin, video_end, audio_end) if end is not None)
    common = [ffmpeg_path, "-v", "error", "-nostdin", "-y", "-copyts"]
    subtitles = listing.parent / ("subtitles" + output_suffix)
    runner.run(
        common + [
            "-i", str(input_path), "-map", "0:s", "-c", "copy",
            "-avoid_negative_ts", "disabled", str(subtitles),
        ],
        None,
    )
    for index, (name, segment_start, segment_end) in enumerate(segments):
        runner.check_cancelled()
        part = listing.parent / Path(name).name
        info = probe_media_info(part, ffprobe_path, runner=runner) or {}
        video = _main_video(info)
        if not video:
            raise VideoSplitError("无法读取 {} 的视频时间戳，字幕未封装。".format(part.name))

        reference = segment_start + origin
        lower = reference if index else origin
        if index + 1 < len(segments):
            upper = segments[index + 1][1] + origin
        else:
            upper = max(segment_end + origin, playback_end)
        if not index and source_video_start is not None:
            reference = max(reference, source_video_start)
        offset = reference - (_seconds(video.get("start_time")) or 0.0)
        # 保留有交集的字幕包，裁剪其时间戳和时长，不改动包内的文字、样式或图像。
        subtitle_filter = (
            "noise=amount=0:drop='gte(pts*tb,{end:.6f})"
            "+lt(pts*tb,{start:.6f})*lte((pts+duration)*tb,{start:.6f})',"
            "setts=ts='(max(PTS*TB,{start:.6f})-({offset:.6f}))/TB':"
            "duration='max(0,min((PTS+DURATION)*TB,{end:.6f})-max(PTS*TB,{start:.6f}))/TB'"
        ).format(start=lower, end=upper, offset=offset)
        runner.run(
            common + [
                "-i", str(part), "-i", str(subtitles), "-map", "0", "-map", "1:s",
                "-c", "copy", "-bsf:s", subtitle_filter,
                "-avoid_negative_ts", "disabled", str(folder / part.name),
            ],
            None,
        )
        part.unlink()


def split_one_video(
    input_path: Path,
    output_root: Path,
    minutes: float,
    mode: str,
    ffmpeg_path: str,
    ffprobe_path: Optional[str],
    runner: FFmpegRunner,
    progress_callback: Optional[Callable[[Optional[float]], None]] = None,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    video_index: int = 1,
    output_group_label: Optional[str] = None,
    custom_name: Optional[str] = None,
    resolution: Optional[Tuple[int, int]] = None,
) -> Tuple[Path, List[Path]]:
    """切分单个视频，返回输出目录和生成的片段。"""
    runner.check_cancelled()
    input_path = input_path.expanduser().resolve()
    if not input_path.is_file():
        raise VideoSplitError("输入文件不存在：{}".format(input_path))
    segment_seconds = minutes * 60.0
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise VideoSplitError("片段时长必须是大于 0 的有限数字")
    if mode not in ("copy", "exact"):
        raise VideoSplitError("未知处理模式：{}".format(mode))
    if resolution is not None and mode == "copy":
        raise VideoSplitError("调整分辨率需要重新编码；请使用精确时长模式。")

    output_root = output_root.expanduser().resolve()
    output_name = render_output_name(
        input_path, name_template, video_index, minutes, custom_name, resolution,
    )
    media_info = probe_media_info(
        input_path, ffprobe_path, count_video_packets=mode == "copy", runner=runner,
    ) or {}
    main_video = _main_video(media_info)
    bounds = _playback_bounds(media_info, input_path, ffprobe_path, runner)
    origin, video_end, audio_end = bounds
    video_tail = 0.0
    if video_end is not None and audio_end is not None:
        tail = audio_end - video_end
        slack = _frame_seconds(main_video)
        if tail > slack:
            if mode == "exact":
                video_tail = tail
            elif int(max(0.0, video_end - origin - slack) / segment_seconds) < int(
                max(0.0, audio_end - origin - slack) / segment_seconds
            ):
                raise VideoSplitError(
                    "音频在画面结束后仍跨越分割切点。请选择精确时长模式补齐画面并保留全部音频。"
                )
    if mode == "copy":
        output_suffix = detected_container_suffix(input_path, media_info)
        ensure_copy_disk_space(input_path, output_root)
    else:
        # 精确模式统一转成标准 H.264/AAC MP4，不再按错误扩展名猜测容器。
        output_suffix = ".mp4"
    runner.check_cancelled()
    folder = create_unique_output_folder(
        output_root,
        input_path,
        output_group_label,
        custom_name,
    )
    try:
        (folder / OUTPUT_MARKER).touch()
        duration = _seconds(media_info.get("format", {}).get("duration"))
        if audio_end is not None:
            duration = max(duration or 0.0, audio_end - origin)
        if video_end is not None:
            duration = max(duration or 0.0, video_end - origin)
        has_subtitles = bool(_streams_of_type(media_info, "subtitle"))
        restore_subtitles = mode == "copy" and ffprobe_path is not None and has_subtitles
        work_context = tempfile.TemporaryDirectory(dir=folder) if restore_subtitles else nullcontext()
        with work_context as work:
            listing = Path(work) / "segments.csv" if work else None
            pattern = (Path(work) if work else folder) / (output_name + output_suffix)
            command = build_ffmpeg_command(
                ffmpeg_path, input_path, pattern, segment_seconds, mode, resolution,
                video_stream_index=main_video.get("index"), video_tail=video_tail,
                segment_list=listing,
            )
            runner.run(command, duration, progress_callback)
            if listing is not None:
                if progress_callback:
                    progress_callback(None)
                _restore_segment_subtitles(
                    ffmpeg_path, ffprobe_path, input_path, folder, listing, output_suffix,
                    bounds, _seconds(main_video.get("start_time")), runner,
                )
        files = sorted(item for item in folder.iterdir() if item.is_file() and item.suffix == output_suffix)
        verify_split_outputs(
            input_path, files, mode, ffprobe_path, resolution,
            segment_seconds=segment_seconds, source_info=media_info, runner=runner,
        )
        runner.check_cancelled()
        if restore_subtitles and progress_callback:
            progress_callback(1.0)
    except (VideoSplitError, OSError):
        cleanup_empty_failed_outputs(folder)
        raise
    return folder, files


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "未知"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, secs)


def run_cli(args: argparse.Namespace) -> int:
    try:
        durations = (
            parse_duration_list(args.durations)
            if args.durations is not None
            else parse_duration_list(str(args.minutes))
        )
        resolutions = parse_resolution_list(
            args.resolutions if args.resolutions is not None else args.resolution
        )
        name_template = validate_name_template(args.name_template)
    except VideoSplitError as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2
    if any(resolution is not None for resolution in resolutions) and args.mode == "copy":
        print(
            "错误：调整分辨率需要重新编码，请同时指定 --mode exact。",
            file=sys.stderr,
        )
        return 2

    ffmpeg_path = find_program("ffmpeg", args.ffmpeg)
    if not ffmpeg_path:
        print(
            "错误：找不到 FFmpeg。请安装 FFmpeg，或用 --ffmpeg 指定可执行文件。",
            file=sys.stderr,
        )
        return 2
    ffprobe_path = companion_ffprobe(ffmpeg_path)
    output_root = Path(args.output).expanduser().resolve()
    failures = 0
    for video_index, item in enumerate(args.inputs, start=1):
        path = Path(item)
        try:
            custom_name = render_custom_name_pattern(
                path,
                args.custom_name_template,
                video_index,
            )
            for warning in compatibility_warnings(path, args.mode, ffprobe_path):
                print("兼容性提醒：{}".format(warning), file=sys.stderr)
        except VideoSplitError as exc:
            failures += len(durations) * len(resolutions)
            print("失败：{}".format(exc), file=sys.stderr)
            continue

        for minutes in durations:
            duration_text = format_minutes_value(minutes)
            for resolution in resolutions:
                resolution_text = format_resolution_label(resolution)
                print(
                    "正在处理：{}（{} 分钟 / {}）".format(
                        path, duration_text, resolution_text
                    )
                )
                runner = FFmpegRunner()

                def report(value: Optional[float]) -> None:
                    if value is not None:
                        print("\r进度：{:5.1f}%".format(value * 100), end="", flush=True)

                try:
                    folder, files = split_one_video(
                        path,
                        output_root,
                        minutes,
                        args.mode,
                        ffmpeg_path,
                        ffprobe_path,
                        runner,
                        report,
                        name_template,
                        video_index,
                        output_group_label(
                            minutes,
                            len(durations),
                            resolution,
                            len(resolutions),
                        ),
                        custom_name,
                        resolution,
                    )
                    print("\n完成：{} 个片段 -> {}".format(len(files), folder))
                except (VideoSplitError, OSError) as exc:
                    failures += 1
                    print(
                        "\n失败（{} 分钟 / {}）：{}".format(
                            duration_text, resolution_text, exc
                        ),
                        file=sys.stderr,
                    )
    return 1 if failures else 0


def launch_gui() -> None:
    # 延迟导入，保证无图形环境下命令行入口仍可工作。
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    class VideoSplitterApp:
        def __init__(self, root: "tk.Tk") -> None:
            self.root = root
            self.root.title(APP_NAME)
            self.root.geometry("980x780")
            self.root.minsize(820, 700)

            self.inputs = []  # type: List[Path]
            self.custom_names = {}  # type: Dict[Path, str]
            self.output_folders = set()
            self.runner = None  # type: Optional[FFmpegRunner]
            self.worker = None  # type: Optional[threading.Thread]
            self.events = queue.Queue()  # type: queue.Queue
            self.closing = False

            self.output_var = tk.StringVar(value=str(Path.cwd() / "output"))
            self.durations_var = tk.StringVar(value="6")
            self.resolutions_var = tk.StringVar(value="原始")
            self.mode_var = tk.StringVar(value="copy")
            self.name_template_var = tk.StringVar(value=DEFAULT_NAME_TEMPLATE)
            self.ffmpeg_var = tk.StringVar(value="")
            self.current_var = tk.StringVar(value="等待添加视频")
            self.overall_var = tk.StringVar(value="0 / 0")

            self._build_ui(ttk, tk, filedialog)
            self.root.after(100, self._poll_events)
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        def _build_ui(self, ttk, tk, filedialog) -> None:
            container = ttk.Frame(self.root, padding=12)
            container.pack(fill="both", expand=True)
            container.columnconfigure(0, weight=1)
            container.rowconfigure(1, weight=1)

            title = ttk.Label(container, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold"))
            title.grid(row=0, column=0, sticky="w", pady=(0, 8))

            files_frame = ttk.LabelFrame(container, text="待处理视频", padding=8)
            files_frame.grid(row=1, column=0, sticky="nsew")
            files_frame.columnconfigure(0, weight=1)
            files_frame.rowconfigure(0, weight=1)

            self.tree = ttk.Treeview(
                files_frame,
                columns=("custom", "status"),
                show="tree headings",
                selectmode="extended",
                height=5,
            )
            self.tree.heading("#0", text="文件")
            self.tree.heading("custom", text="自定义名称（双击修改）")
            self.tree.heading("status", text="状态")
            self.tree.column("#0", width=440, stretch=True)
            self.tree.column("custom", width=240, stretch=True)
            status_sample = ttk.Label(files_frame, text="部分失败 99/99")
            self.tree.column(
                "status", width=status_sample.winfo_reqwidth() + 18,
                anchor="center", stretch=False,
            )
            status_sample.destroy()
            self.tree.bind("<Double-1>", self._edit_custom_name)
            scrollbar = ttk.Scrollbar(files_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scrollbar.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")

            button_bar = ttk.Frame(files_frame)
            button_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
            self.add_button = ttk.Button(button_bar, text="添加视频…", command=self._add_files)
            self.folder_button = ttk.Button(button_bar, text="导入文件夹…", command=self._add_folder)
            self.rename_button = ttk.Button(button_bar, text="批量命名…", command=self._batch_rename)
            self.remove_button = ttk.Button(button_bar, text="移除选中", command=self._remove_selected)
            self.clear_button = ttk.Button(button_bar, text="清空", command=self._clear_files)
            self.add_button.pack(side="left")
            self.folder_button.pack(side="left", padx=(8, 0))
            self.rename_button.pack(side="left", padx=(8, 0))
            self.remove_button.pack(side="left", padx=8)
            self.clear_button.pack(side="left")

            settings = ttk.LabelFrame(container, text="设置", padding=8)
            settings.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            settings.columnconfigure(1, weight=1)

            ttk.Label(settings, text="输出目录").grid(row=0, column=0, sticky="w")
            self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
            self.output_entry.grid(row=0, column=1, sticky="ew", padx=8)
            self.output_button = ttk.Button(
                settings,
                text="浏览…",
                command=lambda: self._choose_output(filedialog),
            )
            self.output_button.grid(row=0, column=2)

            options = ttk.Frame(settings)
            options.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            ttk.Label(options, text="时长方案（分钟）").pack(side="left")
            self.duration_combo = ttk.Combobox(
                options,
                values=("1", "3", "5", "6", "10", "3, 6, 10"),
                width=16,
                textvariable=self.durations_var,
            )
            self.duration_combo.pack(side="left", padx=8)
            ttk.Label(options, text="例如 3, 6, 10").pack(side="left")

            modes = ttk.Frame(settings)
            modes.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            self.exact_radio = ttk.Radiobutton(
                modes,
                text="精确时长（转为兼容 MP4）",
                value="exact",
                variable=self.mode_var,
            )
            self.copy_radio = ttk.Radiobutton(
                modes,
                text="快速无损（默认，保留原编码）",
                value="copy",
                variable=self.mode_var,
            )
            self.copy_radio.pack(side="left")
            self.exact_radio.pack(side="left", padx=(16, 0))

            resolution_options = ttk.Frame(settings)
            resolution_options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            ttk.Label(resolution_options, text="输出分辨率").pack(side="left")
            self.resolution_combo = ttk.Combobox(
                resolution_options,
                values=(
                    "原始",
                    "1920x1080",
                    "1280x720",
                    "854x480",
                    "640x360",
                    "原始, 1280x720",
                ),
                width=22,
                textvariable=self.resolutions_var,
            )
            self.resolution_combo.pack(side="left", padx=8)
            ttk.Label(resolution_options, text="缩放保持显示比例，必要时补黑边").pack(side="left")

            naming = ttk.Frame(settings)
            naming.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            naming.columnconfigure(1, weight=1)
            ttk.Label(naming, text="片段命名模板").grid(row=0, column=0, sticky="w")
            self.name_template_entry = ttk.Entry(naming, textvariable=self.name_template_var)
            self.name_template_entry.grid(row=0, column=1, sticky="ew", padx=8)
            name_help = ttk.Label(
                naming,
                text="{custom} 自定义名称  {name} 原名  {video} 视频序号  "
                     "{duration} 分钟数  {resolution} 分辨率  {index} 片段序号（必填）",
                wraplength=600,
            )
            name_help.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0))
            naming.bind("<Configure>", lambda event: name_help.configure(wraplength=event.width))

            advanced = ttk.Frame(settings)
            advanced.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            advanced.columnconfigure(1, weight=1)
            ttk.Label(advanced, text="FFmpeg（可选）").grid(row=0, column=0, sticky="w")
            self.ffmpeg_entry = ttk.Entry(advanced, textvariable=self.ffmpeg_var)
            self.ffmpeg_entry.grid(row=0, column=1, sticky="ew", padx=8)
            self.ffmpeg_button = ttk.Button(advanced, text="选择…", command=self._choose_ffmpeg)
            self.ffmpeg_button.grid(row=0, column=2)

            progress_frame = ttk.Frame(container)
            progress_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            progress_frame.columnconfigure(0, weight=1)
            ttk.Label(progress_frame, textvariable=self.current_var).grid(row=0, column=0, sticky="w")
            ttk.Label(progress_frame, textvariable=self.overall_var).grid(row=0, column=1, sticky="e")
            self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
            self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))

            actions = ttk.Frame(container)
            actions.grid(row=4, column=0, sticky="e")
            self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel, state="disabled")
            self.start_button = ttk.Button(actions, text="开始分割", command=self._start)
            self.cancel_button.pack(side="left", padx=(0, 8))
            self.start_button.pack(side="left")

        def _add_files(self) -> None:
            selected = filedialog.askopenfilenames(
                title="选择视频文件",
                parent=self.root,
                filetypes=video_filetypes(),
            )
            if selected:
                self._append_inputs(Path(name) for name in selected)

        def _add_folder(self) -> None:
            selected = filedialog.askdirectory(
                title="选择包含视频的文件夹",
                parent=self.root,
            )
            if not selected:
                return
            source = Path(selected).resolve()
            excluded = self.output_folders.copy()
            output_text = self.output_var.get().strip()
            if output_text:
                output_root = Path(output_text).expanduser().resolve()
                if source in output_root.parents:
                    excluded.add(output_root)
            videos = collect_video_files(source, exclude_folders=excluded)
            if not videos:
                messagebox.showinfo(APP_NAME, "该文件夹及其子文件夹中没有找到支持的视频。")
                return
            added = self._append_inputs(videos)
            if added == 0:
                messagebox.showinfo(APP_NAME, "找到的视频均已在待处理列表中。")

        def _append_inputs(self, paths: Iterable[Path]) -> int:
            existing = {str(path).lower() for path in self.inputs}
            added = 0
            for path in paths:
                resolved = path.expanduser().resolve()
                key = str(resolved).lower()
                if resolved.is_file() and key not in existing:
                    self.inputs.append(resolved)
                    self.custom_names[resolved] = safe_stem(resolved)
                    existing.add(key)
                    added += 1
            if not added:
                return 0
            self._refresh_tree()
            self.progress["value"] = 0
            self.overall_var.set("0 / {}".format(len(self.inputs)))
            if self.inputs:
                self.current_var.set(
                    "本次添加 {} 个，共 {} 个视频".format(added, len(self.inputs))
                )
            return added

        def _refresh_tree(self) -> None:
            rows = self.tree.get_children()
            statuses = {
                self.tree.item(item, "text"): self.tree.set(item, "status") for item in rows
            }
            selected_paths = {self.tree.item(item, "text") for item in self.tree.selection()}
            self.tree.delete(*rows)
            for index, path in enumerate(self.inputs):
                custom_name = self.custom_names.get(path, safe_stem(path))
                self.tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    text=str(path),
                    values=(custom_name, statuses.get(str(path), "等待")),
                )
                if str(path) in selected_paths:
                    self.tree.selection_add(str(index))

        def _batch_rename(self) -> None:
            if not self.inputs:
                messagebox.showwarning(APP_NAME, "请先添加至少一个视频文件。")
                return
            selected = sorted(int(item) for item in self.tree.selection())
            indices = selected or list(range(len(self.inputs)))
            initial = (
                self.custom_names.get(self.inputs[indices[0]], safe_stem(self.inputs[indices[0]]))
                if len(indices) == 1
                else "{name}_{video}"
            )
            pattern = simpledialog.askstring(
                APP_NAME,
                "为{}个视频设置名称。\n\n"
                "可使用 {{name}}=原文件名、{{video}}=三位视频序号。\n"
                "示例：实验A_{{video}}".format(len(indices)),
                initialvalue=initial,
                parent=self.root,
            )
            if pattern is None:
                return
            try:
                rendered = {
                    index: render_custom_name_pattern(
                        self.inputs[index],
                        pattern,
                        index + 1,
                    )
                    for index in indices
                }
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            for index, custom_name in rendered.items():
                self.custom_names[self.inputs[index]] = custom_name
            self._refresh_tree()
            self.current_var.set("已更新 {} 个视频的自定义名称".format(len(indices)))

        def _edit_custom_name(self, event) -> None:
            if self.runner:
                return
            if self.tree.identify_region(event.x, event.y) != "cell":
                return
            if self.tree.identify_column(event.x) != "#1":
                return
            item = self.tree.identify_row(event.y)
            if not item:
                return
            index = int(item)
            path = self.inputs[index]
            current = self.custom_names.get(path, safe_stem(path))
            value = simpledialog.askstring(
                APP_NAME,
                "设置“{}”的自定义名称：".format(path.name),
                initialvalue=current,
                parent=self.root,
            )
            if value is None:
                return
            try:
                self.custom_names[path] = validate_custom_name(value)
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            self.tree.set(item, "custom", self.custom_names[path])

        def _remove_selected(self) -> None:
            selected = sorted((int(item) for item in self.tree.selection()), reverse=True)
            if not selected:
                return
            for index in selected:
                self.custom_names.pop(self.inputs[index], None)
                del self.inputs[index]
            self._refresh_tree()
            self.progress["value"] = 0
            self.overall_var.set("0 / {}".format(len(self.inputs)))
            self.current_var.set(
                "待处理列表中有 {} 个视频".format(len(self.inputs))
                if self.inputs else "等待添加视频"
            )

        def _clear_files(self) -> None:
            self.inputs.clear()
            self.custom_names.clear()
            self._refresh_tree()
            self.progress["value"] = 0
            self.current_var.set("等待添加视频")
            self.overall_var.set("0 / 0")

        def _choose_output(self, filedialog_module) -> None:
            selected = filedialog_module.askdirectory(
                title="选择输出目录",
                parent=self.root,
                initialdir=self.output_var.get() or str(Path.cwd()),
            )
            if selected:
                self.output_var.set(selected)

        def _choose_ffmpeg(self) -> None:
            selected = filedialog.askopenfilename(
                title="选择 ffmpeg 可执行文件",
                parent=self.root,
                filetypes=[("FFmpeg", "ffmpeg.exe" if os.name == "nt" else "ffmpeg"), ("所有文件", "*.*")],
            )
            if selected:
                self.ffmpeg_var.set(selected)

        def _set_running(self, running: bool) -> None:
            normal_or_disabled = "disabled" if running else "normal"
            for widget in (
                self.add_button,
                self.folder_button,
                self.rename_button,
                self.remove_button,
                self.clear_button,
                self.output_entry,
                self.output_button,
                self.duration_combo,
                self.resolution_combo,
                self.exact_radio,
                self.copy_radio,
                self.name_template_entry,
                self.ffmpeg_entry,
                self.ffmpeg_button,
            ):
                widget.configure(state=normal_or_disabled)
            self.start_button.configure(state="disabled" if running else "normal")
            self.cancel_button.configure(state="normal" if running else "disabled")

        def _start(self) -> None:
            if not self.inputs:
                messagebox.showwarning(APP_NAME, "请先添加至少一个视频文件。")
                return
            try:
                durations = parse_duration_list(self.durations_var.get())
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                self.duration_combo.focus_set()
                return
            try:
                resolutions = parse_resolution_list(self.resolutions_var.get())
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                self.resolution_combo.focus_set()
                return
            try:
                custom_names = [
                    validate_custom_name(
                        self.custom_names.get(path, safe_stem(path))
                    )
                    for path in self.inputs
                ]
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return

            explicit = self.ffmpeg_var.get().strip() or None
            ffmpeg_path = find_program("ffmpeg", explicit)
            if not ffmpeg_path:
                messagebox.showerror(
                    APP_NAME,
                    "找不到 FFmpeg。\n\n请先安装 FFmpeg，或在“FFmpeg（可选）”中选择 ffmpeg.exe。",
                )
                return

            output_text = self.output_var.get().strip()
            if not output_text:
                messagebox.showerror(APP_NAME, "请选择输出目录。")
                return

            try:
                name_template = validate_name_template(self.name_template_var.get())
                # 使用第一个输入生成一次预览，同时检查非法文件名字符。
                render_output_name(
                    self.inputs[0],
                    name_template,
                    1,
                    durations[0],
                    custom_names[0],
                    resolutions[0],
                )
            except VideoSplitError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                self.name_template_entry.focus_set()
                return

            mode = self.mode_var.get()
            if any(resolution is not None for resolution in resolutions) and mode == "copy":
                if not messagebox.askyesno(
                    APP_NAME,
                    "调整分辨率必须重新编码，不能使用快速无损流复制。\n\n"
                    "软件将自动切换为兼容 H.264/AAC MP4 输出。是否继续？",
                ):
                    return
                mode = "exact"
            ffprobe_path = companion_ffprobe(ffmpeg_path)
            settings = BatchSettings(
                list(self.inputs), custom_names, Path(output_text), durations, resolutions,
                mode, name_template, ffmpeg_path, ffprobe_path,
            )
            previous_status = self.current_var.get()
            self.runner = FFmpegRunner()
            self._set_running(True)
            self.current_var.set("正在检查视频…")
            self.worker = threading.Thread(
                target=self._preflight,
                args=(settings, self.runner, previous_status),
                daemon=True,
            )
            self.worker.start()

        def _preflight(
            self, settings: BatchSettings, runner: FFmpegRunner, previous_status: str,
        ) -> None:
            warnings = []  # type: List[str]
            source_bytes = 0
            error = None
            estimate_size = settings.mode == "copy" and (
                len(settings.durations) * len(settings.resolutions) > 1
            )
            try:
                for input_path in settings.inputs:
                    runner.check_cancelled()
                    if estimate_size:
                        try:
                            source_bytes += input_path.stat().st_size
                        except OSError as exc:
                            warnings.append("{}：无法读取文件大小（{}）".format(input_path.name, exc))
                            continue
                    warnings.extend(
                        compatibility_warnings(
                            input_path, settings.mode, settings.ffprobe_path, runner=runner,
                        )
                    )
            except (VideoSplitError, OSError) as exc:
                error = str(exc)
            self.events.put(("preflight", settings, previous_status, warnings, source_bytes, error))

        def _confirm_batch(
            self, settings: BatchSettings, warnings: List[str], source_bytes: int,
        ) -> bool:
            input_count = len(settings.inputs)
            profile_count = len(settings.durations) * len(settings.resolutions)
            if profile_count > 1:
                duration_text = "、".join(format_minutes_value(item) for item in settings.durations)
                resolution_text = "、".join(format_resolution_label(item) for item in settings.resolutions)
                lines = [
                    "时长方案：{} 分钟".format(duration_text),
                    "分辨率方案：{}".format(resolution_text),
                    "处理任务：{} 个视频 × {} 套方案 = {} 项".format(
                        input_count, profile_count, input_count * profile_count,
                    ),
                ]
                if settings.mode == "copy":
                    lines.append("预计需要约 {:.1f} GB 输出空间。".format(
                        source_bytes * profile_count / (1024 ** 3)
                    ))
                if not messagebox.askyesno(
                    APP_NAME, "\n".join(lines) + "\n\n是否继续？", parent=self.root,
                ):
                    return False
            if not warnings:
                return True
            visible_warnings = warnings[:6]
            if len(warnings) > len(visible_warnings):
                visible_warnings.append("另有 {} 项提醒未显示。".format(len(warnings) - 6))
            return messagebox.askyesno(
                APP_NAME,
                "编码兼容性检查提醒\n\n" + "\n\n".join(visible_warnings) + "\n\n是否继续？",
                parent=self.root,
            )

        def _finish_preflight(
            self, settings: BatchSettings, previous_status: str, warnings: List[str],
            source_bytes: int, error: Optional[str],
        ) -> None:
            runner = self.runner
            if runner.cancelled:
                self._finish_ui([], [], True)
                return
            proceed = not error and self._confirm_batch(settings, warnings, source_bytes)
            if runner.cancelled:
                self._finish_ui([], [], True)
                return
            if not proceed:
                self._stop_batch()
                self.current_var.set(previous_status)
                if error:
                    messagebox.showerror(APP_NAME, error, parent=self.root)
                return
            self.mode_var.set(settings.mode)
            self.progress.configure(mode="determinate")
            self.progress["value"] = 0
            for item in self.tree.get_children():
                self.tree.set(item, "status", "等待")

            self.worker = threading.Thread(
                target=self._worker, args=(*settings, runner), daemon=True,
            )
            self.worker.start()

        def _worker(
            self,
            inputs: List[Path],
            custom_names: List[str],
            output_root: Path,
            durations: List[float],
            resolutions: List[Optional[Tuple[int, int]]],
            mode: str,
            name_template: str,
            ffmpeg_path: str,
            ffprobe_path: Optional[str],
            runner: FFmpegRunner,
        ) -> None:
            results = []  # type: List[Tuple[Path, int]]
            errors = []  # type: List[Tuple[Path, str]]
            variants_per_video = len(durations) * len(resolutions)
            total_jobs = len(inputs) * variants_per_video
            job_number = 0
            for index, input_path in enumerate(inputs):
                completed_variants = 0
                failed_variants = 0
                source_segments = 0
                variant_number = 0
                for minutes in durations:
                    duration_text = format_minutes_value(minutes)
                    for resolution in resolutions:
                        if runner.cancelled:
                            break
                        variant_number += 1
                        job_number += 1
                        resolution_text = format_resolution_label(resolution)
                        variant_description = "{} 分钟 / {}".format(
                            duration_text, resolution_text
                        )
                        self.events.put(
                            (
                                "file_start",
                                index,
                                input_path.name,
                                job_number,
                                total_jobs,
                                variant_description,
                                variant_number,
                                variants_per_video,
                            )
                        )

                        def report(value: Optional[float], file_index: int = index) -> None:
                            self.events.put(("progress", file_index, value))

                        try:
                            folder, files = split_one_video(
                                input_path,
                                output_root,
                                minutes,
                                mode,
                                ffmpeg_path,
                                ffprobe_path,
                                runner,
                                report,
                                name_template,
                                index + 1,
                                output_group_label(
                                    minutes,
                                    len(durations),
                                    resolution,
                                    len(resolutions),
                                ),
                                custom_names[index],
                                resolution,
                            )
                            results.append((folder, len(files)))
                            completed_variants += 1
                            source_segments += len(files)
                        except (VideoSplitError, OSError) as exc:
                            if runner.cancelled:
                                self.events.put(("file_cancelled", index))
                                break
                            failed_variants += 1
                            errors.append((input_path, "{}：{}".format(variant_description, exc)))
                        self.events.put((
                            "file_result", index, completed_variants, failed_variants,
                            variants_per_video, source_segments,
                        ))
                    if runner.cancelled:
                        break
                if runner.cancelled:
                    break
            self.events.put(("finished", results, errors, runner.cancelled))

        def _poll_events(self) -> None:
            try:
                while True:
                    event = self.events.get_nowait()
                    kind = event[0]
                    if kind == "preflight":
                        self._finish_preflight(*event[1:])
                    elif kind == "file_start":
                        _, index, name, job, total, profile_text, variant, variants = event
                        self.current_var.set("正在处理：{}（{}）".format(name, profile_text))
                        self.overall_var.set("{} / {}".format(job, total))
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=0)
                        if self.tree.exists(str(index)):
                            self.tree.set(
                                str(index),
                                "status",
                                "处理中 {}/{}".format(variant, variants),
                            )
                            self.tree.see(str(index))
                    elif kind == "progress":
                        value = event[2]
                        if value is None:
                            if str(self.progress["mode"]) != "indeterminate":
                                self.progress.configure(mode="indeterminate")
                                self.progress.start(12)
                        else:
                            if str(self.progress["mode"]) != "determinate":
                                self.progress.stop()
                                self.progress.configure(mode="determinate")
                            self.progress["value"] = value * 100
                    elif kind == "file_result":
                        _, index, completed, failed, total, count = event
                        if self.tree.exists(str(index)):
                            if failed and completed:
                                status = "部分失败 {}/{}".format(completed, total)
                            elif failed:
                                status = "失败"
                            elif completed == total:
                                status = "完成（{}套/{}段）".format(completed, count)
                            else:
                                status = "完成 {}/{}（{}段）".format(completed, total, count)
                            self.tree.set(str(index), "status", status)
                    elif kind == "file_cancelled":
                        index = event[1]
                        if self.tree.exists(str(index)):
                            self.tree.set(str(index), "status", "已取消")
                    elif kind == "finished":
                        self._finish_ui(event[1], event[2], event[3])
                    if self.closing and self.runner is None:
                        self.root.destroy()
                        return
            except queue.Empty:
                pass
            self.root.after(100, self._poll_events)

        def _stop_batch(self) -> None:
            self.runner = None
            self.worker = None
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._set_running(False)

        def _finish_ui(
            self,
            results: List[Tuple[Path, int]],
            errors: List[Tuple[Path, str]],
            cancelled: bool,
        ) -> None:
            cancelled = cancelled or (self.runner is not None and self.runner.cancelled)
            self._stop_batch()
            self.output_folders.update(folder for folder, _count in results)
            if self.closing:
                return
            total_segments = sum(count for _folder, count in results)
            if cancelled:
                summary = "处理已取消，已保留 {} 套结果".format(len(results))
            elif errors:
                summary = "成功 {} 项，失败 {} 项".format(len(results), len(errors))
            else:
                self.progress["value"] = 100
                summary = "全部完成，共生成 {} 套、{} 个片段".format(
                    len(results), total_segments,
                )
            self.current_var.set(summary)
            details = []
            if results:
                details.append("输出目录\n" + "\n".join(
                    str(folder) for folder, _count in results
                ))
            if errors:
                details.append("失败详情\n\n" + "\n\n".join(
                    "{}\n{}".format(path, message) for path, message in errors
                ))
            if details:
                self._show_results(summary, "\n\n".join(details))
            else:
                messagebox.showinfo(APP_NAME, summary, parent=self.root)

        def _show_results(self, summary: str, details: str) -> None:
            dialog = tk.Toplevel(self.root)
            dialog.title("分割结果")
            dialog.transient(self.root)
            dialog.geometry("720x420")
            dialog.minsize(480, 260)
            frame = ttk.Frame(dialog, padding=12)
            frame.pack(fill="both", expand=True)
            frame.rowconfigure(1, weight=1)
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=summary).grid(row=0, column=0, sticky="w", pady=(0, 8))
            view = tk.Text(frame, wrap="char", padx=8, pady=8)
            scrollbar = ttk.Scrollbar(frame, command=view.yview)
            view.configure(yscrollcommand=scrollbar.set)
            view.insert("1.0", details)
            view.configure(state="disabled")
            view.grid(row=1, column=0, sticky="nsew")
            scrollbar.grid(row=1, column=1, sticky="ns")
            ttk.Button(frame, text="关闭", command=dialog.destroy).grid(
                row=2, column=0, columnspan=2, sticky="e", pady=(8, 0),
            )
            dialog.bind("<Escape>", lambda _event: dialog.destroy())

        def _cancel(self) -> None:
            if self.runner:
                self.current_var.set("正在取消…")
                self.cancel_button.configure(state="disabled")
                self.runner.cancel()

        def _on_close(self) -> None:
            if self.runner:
                if self.closing or not messagebox.askyesno(
                    APP_NAME, "视频仍在处理中，确定要取消并退出吗？", parent=self.root,
                ):
                    return
                self.closing = True
                self._cancel()
            else:
                self.root.destroy()

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    VideoSplitterApp(root)
    root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            f"{APP_NAME}支持按多种时长切分视频，并按多种分辨率转码。"
            "不传入输入文件时启动图形界面。"
        )
    )
    parser.add_argument("inputs", nargs="*", help="一个或多个视频文件")
    parser.add_argument("-o", "--output", default="output", help="输出根目录（默认：output）")
    parser.add_argument(
        "-m",
        "--minutes",
        type=float,
        default=DEFAULT_MINUTES,
        help="每段分钟数（默认：6）",
    )
    parser.add_argument(
        "--durations",
        help="批量时长方案，使用逗号分隔，例如：3,6,10（设置后覆盖 --minutes）",
    )
    parser.add_argument(
        "--resolution",
        default="原始",
        help="输出分辨率，例如 1280x720；默认保持原始分辨率",
    )
    parser.add_argument(
        "--resolutions",
        help="批量分辨率方案，使用逗号分隔，例如：原始,1280x720（覆盖 --resolution）",
    )
    parser.add_argument(
        "--mode",
        choices=("exact", "copy"),
        default="copy",
        help="copy=无损流复制，exact=精确切分并转码（默认：copy）",
    )
    parser.add_argument("--ffmpeg", help="ffmpeg 或其所在目录的路径")
    parser.add_argument(
        "--custom-name-template",
        default=DEFAULT_CUSTOM_NAME_TEMPLATE,
        help=(
            "批量生成每个源视频的自定义名称，可使用 {name}、{video}；"
            "默认：{name}"
        ),
    )
    parser.add_argument(
        "--name-template",
        default=DEFAULT_NAME_TEMPLATE,
        help=(
            "片段命名模板，可使用 {name}、{custom}、{video}、{duration}、"
            "{resolution}、{index}；"
            "必须且只能包含一个 {index}（默认：{custom}_片段_{index}）"
        ),
    )
    parser.add_argument("--gui", action="store_true", help="强制启动图形界面")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gui or not args.inputs:
        launch_gui()
        return 0
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
