from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from shortbridge.source import SourceError, download_source, inspect_source, metadata_from_document


class SourceMetadataTests(unittest.TestCase):
    def test_youtube_preserves_title_and_body(self) -> None:
        metadata = metadata_from_document(
            {
                "extractor_key": "Youtube",
                "id": "abc123",
                "webpage_url": "https://www.youtube.com/shorts/abc123",
                "title": "A useful title",
                "description": "First line\nSecond line",
                "duration": 42,
                "uploader": "Example channel",
            }
        )

        self.assertEqual(metadata.platform, "youtube")
        self.assertEqual(metadata.title, "A useful title")
        self.assertEqual(metadata.body, "First line\nSecond line")
        self.assertEqual(metadata.tiktok_caption, "A useful title\n\nFirst line\nSecond line")

    def test_instagram_uses_description_without_generated_title(self) -> None:
        metadata = metadata_from_document(
            {
                "extractor_key": "Instagram",
                "id": "xyz789",
                "webpage_url": "https://www.instagram.com/reel/xyz789/",
                "title": "Video by an account",
                "description": "Original reel description #example",
                "duration": 12.5,
                "uploader": "example",
            }
        )

        self.assertEqual(metadata.platform, "instagram")
        self.assertIsNone(metadata.title)
        self.assertEqual(metadata.body, "Original reel description #example")
        self.assertEqual(metadata.tiktok_caption, "Original reel description #example")

    def test_inspect_rejects_unsupported_hosts_before_running_downloader(self) -> None:
        called = False

        def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal called
            called = True
            raise AssertionError("runner should not be called")

        with self.assertRaisesRegex(SourceError, "YouTube or Instagram"):
            inspect_source("https://example.com/video", runner=runner)

        self.assertFalse(called)

    def test_inspect_reads_machine_output_from_yt_dlp(self) -> None:
        document = {
            "extractor_key": "Youtube",
            "id": "abc123",
            "webpage_url": "https://youtu.be/abc123",
            "title": "Title",
            "description": "Body",
            "duration": 10,
        }

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[0], "yt-dlp")
            self.assertIn("--dump-single-json", command)
            return subprocess.CompletedProcess(command, 0, json.dumps(document), "")

        metadata = inspect_source("https://youtu.be/abc123", runner=runner)

        self.assertEqual(metadata.source_id, "abc123")
        self.assertEqual(metadata.tiktok_caption, "Title\n\nBody")

    def test_inspect_can_use_server_cookie_file(self) -> None:
        document = {
            "extractor_key": "Instagram",
            "id": "reel-1",
            "webpage_url": "https://www.instagram.com/reel/reel-1/",
            "description": "Description",
        }
        cookies_path = Path("/run/secrets/yt_dlp_cookies")

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            cookie_index = command.index("--cookies")
            self.assertEqual(command[cookie_index + 1], str(cookies_path))
            return subprocess.CompletedProcess(command, 0, json.dumps(document), "")

        metadata = inspect_source(
            "https://www.instagram.com/reel/reel-1/",
            cookies_path=cookies_path,
            runner=runner,
        )

        self.assertEqual(metadata.body, "Description")

    def test_download_returns_video_and_preserved_text(self) -> None:
        document = {
            "extractor_key": "Youtube",
            "id": "abc123",
            "webpage_url": "https://youtu.be/abc123",
            "title": "Saved title",
            "description": "Saved body",
            "duration": 10,
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)

            def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertIn("--write-info-json", command)
                self.assertIn("--merge-output-format", command)
                (destination / "job-1.mp4").write_bytes(b"video")
                (destination / "job-1.info.json").write_text(json.dumps(document))
                return subprocess.CompletedProcess(command, 0, "", "")

            asset = download_source(
                "https://youtu.be/abc123",
                destination=destination,
                stem="job-1",
                runner=runner,
            )

        self.assertEqual(asset.media_path.name, "job-1.mp4")
        self.assertEqual(asset.metadata.title, "Saved title")
        self.assertEqual(asset.metadata.body, "Saved body")


if __name__ == "__main__":
    unittest.main()
