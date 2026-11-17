import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_splitter import (
    FFmpegRunner,
    VideoSplitError,
    build_ffmpeg_command,
    build_parser,
    cleanup_empty_failed_outputs,
    collect_video_files,
    companion_ffprobe,
    compatibility_warnings,
    create_unique_output_folder,
    detected_container_suffix,
    find_program,
    format_minutes_value,
    format_resolution_label,
    output_group_label,
    output_pattern,
    parse_duration_list,
    parse_resolution_list,
    probe_media_info,
    render_custom_name_pattern,
    render_output_name,
    safe_stem,
    split_one_video,
    validate_custom_name,
    validate_name_template,
    verify_split_outputs,
    video_filetypes,
)


class VideoSplitterTests(unittest.TestCase):
    def test_multiple_duration_values_are_parsed_and_deduplicated(self):
        self.assertEqual(parse_duration_list("3, 6，10; 6 3"), [3.0, 6.0, 10.0])

    def test_duration_values_must_be_positive_finite_numbers(self):
        for value in ("", "0", "-1", "abc", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(VideoSplitError):
                parse_duration_list(value)

    def test_duration_value_is_safe_for_names(self):
        self.assertEqual(format_minutes_value(6.0), "6")
        self.assertEqual(format_minutes_value(0.5), "0.5")

    def test_resolution_values_are_parsed_and_deduplicated(self):
        self.assertEqual(
            parse_resolution_list("原始, 1280x720；1280×720, 640X360"),
            [None, (1280, 720), (640, 360)],
        )

    def test_resolution_values_require_even_valid_dimensions(self):
        for value in ("", "720p", "0x720", "1279x720", "1280x719"):
            with self.subTest(value=value), self.assertRaises(VideoSplitError):
                parse_resolution_list(value)

    def test_resolution_labels_and_group_names_are_stable(self):
        self.assertEqual(format_resolution_label(None), "原始")
        self.assertEqual(format_resolution_label((1280, 720)), "1280x720")
        self.assertEqual(output_group_label(6, 1, None, 1), None)
        self.assertEqual(
            output_group_label(3, 2, (1280, 720), 2),
            "3分钟_1280x720",
        )

    def test_batch_custom_name_pattern_uses_source_and_video_number(self):
        self.assertEqual(
            render_custom_name_pattern(Path("原始文件.mp4"), "实验_{video}_{name}", 7),
            "实验_007_原始文件",
        )

    def test_custom_name_rejects_invalid_filename_characters(self):
        for value in ("", "bad/name", "100%", "CON"):
            with self.subTest(value=value), self.assertRaises(VideoSplitError):
                validate_custom_name(value)

    def test_output_pattern_keeps_extension_and_numbering(self):
        folder = Path("results")
        source = Path("示例视频.MP4")
        self.assertEqual(
            output_pattern(folder, source),
            folder / "示例视频_片段_%03d.mp4",
        )

    def test_percent_in_source_name_is_sanitized(self):
        self.assertEqual(safe_stem(Path("100%完成.mov")), "100_完成")

    def test_file_dialog_uses_native_multiple_patterns(self):
        label, patterns = video_filetypes()[0]
        self.assertEqual(label, "所有支持的视频")
        self.assertIsInstance(patterns, tuple)
        self.assertIn("*.mp4", patterns)
        self.assertIn("*.mkv", patterns)

    def test_collect_video_files_includes_subfolders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "课程"
            nested.mkdir()
            first = root / "第一讲.mp4"
            second = nested / "第二讲.MOV"
            ignored = nested / "说明.txt"
            first.touch()
            second.touch()
            ignored.touch()
            self.assertEqual(collect_video_files(root), [first, second])

    def test_custom_name_template_is_uniform_and_numbered(self):
        pattern = output_pattern(
            Path("results"),
            Path("任意原名.mp4"),
            "课程_{video}_片段_{index}",
            4,
        )
        self.assertEqual(pattern, Path("results") / "课程_004_片段_%03d.mp4")

    def test_duration_can_be_used_in_name_template(self):
        pattern = output_pattern(
            Path("results"),
            Path("课程.mp4"),
            "{name}_{duration}分钟_{index}",
            duration_minutes=2.5,
        )
        self.assertEqual(pattern, Path("results") / "课程_2.5分钟_%03d.mp4")

    def test_custom_source_name_can_be_used_in_output_template(self):
        pattern = output_pattern(
            Path("results"),
            Path("原文件.mp4"),
            "{custom}_{duration}分钟_{index}",
            duration_minutes=6,
            custom_name="实验A_001",
        )
        self.assertEqual(pattern, Path("results") / "实验A_001_6分钟_%03d.mp4")

    def test_resolution_can_be_used_in_output_template(self):
        pattern = output_pattern(
            Path("results"),
            Path("原文件.mp4"),
            "{custom}_{resolution}_{index}",
            custom_name="实验A",
            resolution=(1280, 720),
        )
        self.assertEqual(pattern, Path("results") / "实验A_1280x720_%03d.mp4")

    def test_name_template_requires_segment_index(self):
        with self.assertRaises(VideoSplitError):
            validate_name_template("只有统一名称")
        with self.assertRaises(VideoSplitError):
            validate_name_template("{index}_{index}")

    def test_name_template_rejects_unknown_field(self):
        with self.assertRaises(VideoSplitError):
            render_output_name(Path("demo.mp4"), "{date}_{index}")

    def test_copy_mode_uses_stream_copy_and_excludes_data_streams(self):
        command = build_ffmpeg_command(
            "ffmpeg",
            Path("input.mkv"),
            Path("out_%03d.mkv"),
            360,
            "copy",
        )
        self.assertIn("copy", command)
        mapped_streams = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-map"
        ]
        self.assertEqual(mapped_streams, ["0:V:0", "0:a?", "0:s?"])
        self.assertIn("-dn", command)
        self.assertNotIn("-force_key_frames", command)
        self.assertEqual(command[-1], "out_%03d.mkv")

    def test_copy_mode_is_the_default(self):
        self.assertEqual(build_parser().parse_args([]).mode, "copy")

    def test_exact_mode_forces_six_minute_keyframes(self):
        command = build_ffmpeg_command(
            "ffmpeg",
            Path("input.mp4"),
            Path("out_%03d.mp4"),
            360,
            "exact",
        )
        keyframe_index = command.index("-force_key_frames")
        self.assertIn("360.000000", command[keyframe_index + 1])
        self.assertIn("libx264", command)
        self.assertIn("-segment_format_options", command)
        options = command[command.index("-segment_format_options") + 1]
        self.assertIn("movflags=+faststart:video_track_timescale=90000:brand=mp42", options)
        self.assertIn("avoid_negative_ts=make_non_negative", options)
        segment_index = command.index("-segment_time")
        self.assertEqual(command[segment_index + 1], "360.000000")

    def test_exact_mode_rejects_unknown_container(self):
        with self.assertRaises(VideoSplitError):
            build_ffmpeg_command(
                "ffmpeg",
                Path("input.xyz"),
                Path("out_%03d.xyz"),
                360,
                "exact",
            )

    def test_exact_mode_converts_avi_input_to_standard_mp4(self):
        command = build_ffmpeg_command(
            "ffmpeg",
            Path("input.avi"),
            Path("out_%03d.mp4"),
            360,
            "exact",
        )
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertNotIn("mpeg4", command)
        self.assertNotIn("libmp3lame", command)

    def test_exact_mode_resizes_without_stretching(self):
        command = build_ffmpeg_command(
            "ffmpeg",
            Path("input.mp4"),
            Path("out_%03d.mp4"),
            360,
            "exact",
            (1280, 720),
        )
        filter_value = command[command.index("-vf") + 1]
        self.assertIn("720*dar", filter_value)
        self.assertIn("1280/dar", filter_value)
        self.assertIn("pad=1280:720", filter_value)
        self.assertIn("setsar=1", filter_value)

    def test_high_resolution_leaves_level_selection_to_encoder(self):
        command = build_ffmpeg_command(
            "ffmpeg",
            Path("input.mp4"),
            Path("out_%03d.mp4"),
            360,
            "exact",
            (3840, 2160),
        )
        self.assertNotIn("-level:v", command)

    def test_copy_mode_cannot_change_resolution(self):
        with self.assertRaises(ValueError):
            build_ffmpeg_command(
                "ffmpeg",
                Path("input.mp4"),
                Path("out_%03d.mp4"),
                360,
                "copy",
                (1280, 720),
            )

    def test_exact_profiles_never_use_mpeg4_or_mp3(self):
        for suffix in (".mp4", ".mkv", ".mov", ".webm", ".ts", ".3gp"):
            command = build_ffmpeg_command(
                "ffmpeg",
                Path("input" + suffix),
                Path("out_%03d.mp4"),
                360,
                "exact",
            )
            self.assertNotIn("mpeg4", command)
            self.assertNotIn("libmp3lame", command)

    def test_copy_mode_warns_about_mp4v_and_mp3_source(self):
        info = {
            "format": {"duration": "60"},
            "streams": [
                {"codec_type": "video", "codec_name": "mpeg4", "codec_tag_string": "mp4v"},
                {"codec_type": "audio", "codec_name": "mp3", "codec_tag_string": "mp4a"},
            ],
        }
        with mock.patch("video_splitter.probe_media_info", return_value=info):
            warnings = compatibility_warnings(Path("legacy.mp4"), "copy", "ffprobe")
        self.assertEqual(len(warnings), 2)
        self.assertIn("mp4v", warnings[0])
        self.assertIn("MP3", warnings[1])

    def test_mislabeled_mp4_is_detected_as_avi(self):
        info = {"format": {"format_name": "avi"}, "streams": []}
        self.assertEqual(detected_container_suffix(Path("camera.mp4"), info), ".avi")
        self.assertEqual(
            output_pattern(
                Path("results"),
                Path("camera.mp4"),
                output_suffix=".avi",
            ),
            Path("results") / "camera_片段_%03d.avi",
        )

    def test_copy_mode_warns_and_corrects_mismatched_container_extension(self):
        info = {
            "format": {"format_name": "avi"},
            "streams": [
                {"codec_type": "video", "codec_name": "rawvideo", "codec_tag_string": "YUY2"},
                {"codec_type": "audio", "codec_name": "pcm_s16le", "codec_tag_string": "[1][0][0][0]"},
            ],
        }
        with mock.patch("video_splitter.probe_media_info", return_value=info):
            warnings = compatibility_warnings(Path("camera.mp4"), "copy", "ffprobe")
        self.assertEqual(len(warnings), 1)
        self.assertIn("实际容器是 AVI", warnings[0])
        self.assertIn(".avi", warnings[0])

    def test_lossless_verification_uses_packets_instead_of_container_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "part.mp4"
            source.write_bytes(b"s" * (100 * 1024))
            output.write_bytes(b"o" * (10 * 1024))
            input_info = {"streams": [{"codec_type": "video", "nb_read_packets": "200"}]}
            output_info = {
                "format": {"duration": "4"},
                "streams": [{"codec_type": "video", "nb_read_packets": "200"}],
            }
            with mock.patch("video_splitter.probe_media_info", return_value=output_info):
                verify_split_outputs(source, [output], "copy", "ffprobe", source_info=input_info)
                output_info["streams"][0]["nb_read_packets"] = "100"
                with self.assertRaisesRegex(VideoSplitError, "200"):
                    verify_split_outputs(source, [output], "copy", "ffprobe", source_info=input_info)

    def test_lossless_verification_reports_when_keyframes_prevent_cutting(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "part.mp4"
            output.write_bytes(b"video")
            info = {
                "format": {"duration": "721"},
                "streams": [{"codec_type": "video", "nb_read_packets": "1442"}],
            }
            with mock.patch("video_splitter.probe_media_info", return_value=info):
                with self.assertRaisesRegex(VideoSplitError, "关键帧"):
                    verify_split_outputs(
                        Path("source.mp4"), [output], "copy", "ffprobe", segment_seconds=360,
                    )

    def test_folder_import_skips_excluded_output_subtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.touch()
            nested_output = root / "output" / "segments"
            nested_output.mkdir(parents=True)
            (nested_output / "segment.mp4").touch()
            self.assertEqual(collect_video_files(root, exclude_folders=[nested_output.parent]), [source])

    def test_failed_zero_byte_outputs_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "failed"
            folder.mkdir()
            (folder / "part_001.mp4").touch()
            cleanup_empty_failed_outputs(folder)
            self.assertFalse(folder.exists())

    def test_unique_output_folder_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = create_unique_output_folder(root, Path("demo.mp4"))
            second = create_unique_output_folder(root, Path("demo.mp4"))
            self.assertEqual(first.name, "demo_片段")
            self.assertEqual(second.name, "demo_片段_2")
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_multi_duration_folder_has_duration_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = create_unique_output_folder(root, Path("demo.mp4"), "2.5分钟")
            self.assertEqual(folder.name, "demo_片段_2.5分钟")

    def test_custom_name_is_used_for_output_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = create_unique_output_folder(
                root,
                Path("demo.mp4"),
                custom_name="实验A_001",
            )
            self.assertEqual(folder.name, "实验A_001_片段")

    def test_split_workflow_returns_generated_segments(self):
        class FakeRunner(FFmpegRunner):
            def run(self, command, duration, progress_callback):
                pattern = Path(command[-1])
                Path(str(pattern).replace("%03d", "001")).write_bytes(b"1" * 600)
                Path(str(pattern).replace("%03d", "002")).write_bytes(b"2" * 600)
                if progress_callback:
                    progress_callback(1.0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "demo.mp4"
            source.write_bytes(b"s" * 1000)
            progress = []
            folder, files = split_one_video(
                source,
                root / "output",
                6,
                "copy",
                "ffmpeg",
                None,
                FakeRunner(),
                progress.append,
            )
            self.assertEqual(folder.name, "demo_片段")
            self.assertEqual([item.name for item in files], [
                "demo_片段_001.mp4",
                "demo_片段_002.mp4",
            ])
            self.assertEqual(progress, [1.0])


class FFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = find_program("ffmpeg")
        cls.ffprobe = companion_ffprobe(cls.ffmpeg) if cls.ffmpeg else None
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("需要 FFmpeg 和 FFprobe")

    def run_ffmpeg(self, *args):
        return subprocess.run(
            [self.ffmpeg, "-v", "error", *map(str, args)],
            check=True, capture_output=True, timeout=30,
        ).stdout

    def test_mp4_segments_keep_audio_at_cut_points_and_allow_percent_output_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=13",
                "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=13",
                "-c:v", "libx264", "-g", "50", "-c:a", "aac", source,
            )

            def decoded_samples(path):
                return len(self.run_ffmpeg("-i", path, "-map", "0:a:0", "-f", "s16le", "-")) // 2

            original = decoded_samples(source)
            for mode in ("copy", "exact"):
                with self.subTest(mode=mode):
                    _, parts = split_one_video(
                        source, root / ("100%" + mode), 0.1, mode,
                        self.ffmpeg, self.ffprobe, FFmpegRunner(),
                    )
                    self.assertEqual(len(parts), 3)
                    self.assertGreaterEqual(sum(decoded_samples(part) for part in parts), original - 48)

    def test_mkv_with_wrong_extension_keeps_matroska_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=s=80x48:r=25:d=1",
                "-c:v", "libx264", "-f", "matroska", source,
            )
            _, parts = split_one_video(
                source, root / "output", 6, "copy", self.ffmpeg, self.ffprobe, FFmpegRunner(),
            )
            self.assertEqual(parts[0].suffix, ".mkv")
            self.assertIn("matroska", probe_media_info(parts[0], self.ffprobe)["format"]["format_name"])

    def test_both_modes_select_default_video_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "tracks.mkv"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=red:s=64x48:r=25:d=1",
                "-f", "lavfi", "-i", "color=green:s=160x90:r=25:d=1",
                "-map", "0:v", "-map", "1:v", "-disposition:v:0", "0",
                "-disposition:v:1", "default", "-c:v", "libx264", source,
            )
            for mode in ("copy", "exact"):
                with self.subTest(mode=mode):
                    _, parts = split_one_video(
                        source, root / mode, 6, mode, self.ffmpeg, self.ffprobe, FFmpegRunner(),
                    )
                    videos = probe_media_info(parts[0], self.ffprobe)["streams"]
                    self.assertEqual([stream["width"] for stream in videos], [160])

    def test_copy_reports_audio_tail_and_exact_pads_video_to_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "long-audio.mp4"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=gray:s=96x64:r=25:d=2",
                "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=13",
                "-c:v", "libx264", "-c:a", "aac", source,
            )
            with self.assertRaisesRegex(VideoSplitError, "请选择精确时长模式"):
                split_one_video(
                    source, root / "copy", 0.1, "copy", self.ffmpeg, self.ffprobe, FFmpegRunner(),
                )
            self.assertFalse((root / "copy").exists())
            _, parts = split_one_video(
                source, root / "exact", 0.1, "exact", self.ffmpeg, self.ffprobe, FFmpegRunner(),
            )
            self.assertEqual(len(parts), 3)
            self.assertEqual(
                [float(probe_media_info(part, self.ffprobe)["streams"][0]["duration"]) for part in parts],
                [6.0, 6.0, 1.0],
            )
            last_audio = probe_media_info(parts[-1], self.ffprobe)["streams"][1]
            self.assertGreater(float(last_audio["duration"]), 0.9)

    def test_exact_cut_does_not_use_an_early_natural_keyframe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "60fps.mp4"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=gray:s=96x64:r=60:d=9",
                "-c:v", "libx264", source,
            )
            _, parts = split_one_video(
                source, root / "output", 0.07, "exact",
                self.ffmpeg, self.ffprobe, FFmpegRunner(),
            )
            self.assertEqual(
                [float(probe_media_info(part, self.ffprobe)["streams"][0]["duration"]) for part in parts],
                [4.2, 4.2, 0.6],
            )

    def test_audio_tail_without_stream_durations_uses_packet_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "long-audio.flv"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=gray:s=96x64:r=25:d=2",
                "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=13",
                "-c:v", "libx264", "-c:a", "aac", "-flvflags", "no_duration_filesize", source,
            )
            with self.assertRaisesRegex(VideoSplitError, "请选择精确时长模式"):
                split_one_video(
                    source, root / "copy", 0.1, "copy", self.ffmpeg, self.ffprobe, FFmpegRunner(),
                )
            _, parts = split_one_video(
                source, root / "exact", 0.1, "exact", self.ffmpeg, self.ffprobe, FFmpegRunner(),
            )
            self.assertEqual(len(parts), 3)
            last_audio = probe_media_info(parts[-1], self.ffprobe)["streams"][1]
            self.assertGreater(float(last_audio["duration"]), 1.0)

    def test_subtitles_cross_the_cut_with_original_text_and_clipped_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subtitle = root / "captions.srt"
            subtitle.write_text(
                "1\n00:00:05,000 --> 00:00:09,000\nCROSSING\n\n"
                "2\n00:00:10,000 --> 00:00:11,000\nLATER\n",
                encoding="utf-8",
            )
            for suffix, codec in (("mkv", "srt"), ("mp4", "mov_text")):
                with self.subTest(suffix=suffix):
                    source = root / ("source." + suffix)
                    self.run_ffmpeg(
                        "-f", "lavfi", "-i", "testsrc2=s=96x64:r=25:d=13",
                        "-i", subtitle, "-c:v", "libx264", "-g", "25", "-sc_threshold", "0",
                        "-c:s", codec, source,
                    )
                    _, parts = split_one_video(
                        source, root / ("out%" + suffix), 0.1, "copy",
                        self.ffmpeg, self.ffprobe, FFmpegRunner(),
                    )
                    self.assertEqual(len(parts), 3)
                    captions = [
                        self.run_ffmpeg("-copyts", "-i", part, "-map", "0:s:0", "-f", "srt", "-")
                        .decode("utf-8")
                        for part in parts
                    ]
                    self.assertRegex(captions[0], r"00:00:06,\d{3}\s+CROSSING")
                    self.assertRegex(captions[1], r"00:00:00,\d{3} --> 00:00:03,\d{3}\s+CROSSING")
                    self.assertIn("LATER", captions[1])
                    self.assertFalse(captions[2].strip())

    def test_import_skips_previous_outputs_in_source_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.mp4"
            self.run_ffmpeg(
                "-f", "lavfi", "-i", "color=gray:s=96x64:r=25:d=1",
                "-c:v", "libx264", source,
            )
            legacy = root / "source_片段"
            legacy.mkdir()
            (legacy / "source_片段_001.mp4").touch()
            result_folder, _ = split_one_video(
                source, root, 6, "copy", self.ffmpeg, self.ffprobe, FFmpegRunner(),
                name_template="export_{index}", custom_name="Custom name",
            )
            self.assertEqual(collect_video_files(root), [source])
            self.assertEqual([part.name for part in collect_video_files(result_folder)], ["export_001.mp4"])


if __name__ == "__main__":
    unittest.main()
