import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from recording_naming import (
    candidate_media_paths,
    find_existing_complete_path,
    legacy_media_filename,
    media_filename,
    safe_name,
    segment_indexes,
    zoom_legacy_filename,
    zoom_media_filename,
)


class RecordingNamingTests(unittest.TestCase):
    def test_media_filename_uses_full_title_and_chinese_view(self):
        self.assertEqual(
            media_filename("01-CPTSD评估干预实操（2025）", 0),
            "01-CPTSD评估干预实操（2025）_混合画面.mp4",
        )
        self.assertEqual(media_filename("课程", 1), "课程_共享屏幕.mp4")
        self.assertEqual(media_filename("课程", 2), "课程_讲者摄像头.mp4")

    def test_multisegment_filename_is_zero_padded(self):
        self.assertEqual(media_filename("课程", 1, segment_index=2), "课程_第02段_共享屏幕.mp4")
        self.assertEqual(legacy_media_filename(1, segment_index=2), "screen_2.mp4")

    def test_safe_name_removes_invalid_path_characters(self):
        self.assertEqual(safe_name('课程: A/B? "测试"'), "课程_ A_B_ _测试_")

    def test_candidates_prefer_full_name_then_legacy_name(self):
        paths = candidate_media_paths("/recording", "课程", 0)
        self.assertEqual(paths[0].name, "课程_混合画面.mp4")
        self.assertEqual(paths[1].name, "mixed.mp4")

    def test_string_resource_labels_map_to_legacy_names(self):
        self.assertEqual(media_filename("课程", "screen"), "课程_共享屏幕.mp4")
        self.assertEqual(legacy_media_filename("screen"), "screen.mp4")

    def test_segment_indexes_group_streams_by_recording_id(self):
        versions = [
            {"recording_id": "a", "stream_type": 0},
            {"recording_id": "a", "stream_type": 1},
            {"recording_id": "a", "stream_type": 2},
            {"recording_id": "b", "stream_type": 0},
            {"recording_id": "b", "stream_type": 2},
        ]
        self.assertEqual(segment_indexes(versions), {"a": 1, "b": 2})

    def test_single_recording_has_no_segment_suffix(self):
        versions = [
            {"recording_id": "a", "stream_type": 0},
            {"recording_id": "a", "stream_type": 1},
        ]
        self.assertEqual(segment_indexes(versions), {"a": None})

    def test_existing_complete_path_falls_back_to_legacy_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "mixed.mp4"
            legacy.write_bytes(b"12345")
            found = find_existing_complete_path(tmp, "课程", 0, expected_size=5)
            self.assertEqual(found, legacy)

    def test_incomplete_file_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "课程_混合画面.mp4"
            current.write_bytes(b"12")
            self.assertIsNone(find_existing_complete_path(tmp, "课程", 0, expected_size=5))

    def test_zoom_filenames_prefixed_with_title(self):
        self.assertEqual(zoom_media_filename("BCYP-6", "video"), "BCYP-6_video.mp4")
        self.assertEqual(
            zoom_media_filename("BCYP-6", "audio", lang="中文-CN"),
            "BCYP-6_audio__中文-CN.m4a",
        )
        self.assertEqual(
            zoom_media_filename("BCYP-6", "caption", rec="rec1"),
            "BCYP-6_caption_rec1.vtt",
        )
        self.assertEqual(zoom_media_filename("BCYP-6", "chapter"), "BCYP-6_chapter.json")

    def test_zoom_legacy_filenames_stay_short(self):
        self.assertEqual(zoom_legacy_filename("video"), "video.mp4")
        self.assertEqual(zoom_legacy_filename("audio", lang="English-US"), "audio__English-US.m4a")
        self.assertEqual(zoom_legacy_filename("caption", rec="rec2"), "caption_rec2.vtt")


if __name__ == "__main__":
    unittest.main()
