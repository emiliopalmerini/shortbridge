from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortbridge import cli
from shortbridge.cli import main
from shortbridge.queue import JobQueue
from shortbridge.source import SourceAsset, SourceMetadata


class CliTests(unittest.TestCase):
    def test_help_leads_with_examples(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(["--help"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertIn("examples:", stdout.getvalue().lower())
        self.assertIn("schedule", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_missing_command_is_actionable(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main([], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Choose a command", stderr.getvalue())
        self.assertIn("shortbridge --help", stderr.getvalue())

    def test_version_is_primary_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(["--version"], stdout=stdout, stderr=stderr)

        self.assertRegex(stdout.getvalue(), r"^shortbridge \d+\.\d+\.\d+\n$")
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_inspect_json_emits_source_text_on_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        metadata = SourceMetadata(
            platform="youtube",
            source_id="abc123",
            source_url="https://youtu.be/abc123",
            title="Title",
            body="Body",
            duration_seconds=15,
            uploader="Channel",
            raw={},
        )

        with patch.object(cli, "inspect_source", return_value=metadata):
            exit_code = main(
                ["--json", "inspect", "https://youtu.be/abc123"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["source_body"], "Body")
        self.assertEqual(stderr.getvalue(), "")

    def test_schedule_add_downloads_and_persists_source_text(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)

            def fake_download(
                url: str,
                *,
                destination: Path,
                stem: str,
            ) -> SourceAsset:
                media_path = destination / f"{stem}.mp4"
                metadata_path = destination / f"{stem}.info.json"
                destination.mkdir(parents=True, exist_ok=True)
                media_path.write_bytes(b"video")
                metadata_path.write_text("{}")
                metadata = SourceMetadata(
                    platform="instagram",
                    source_id="reel-1",
                    source_url=url,
                    title=None,
                    body="Original description",
                    duration_seconds=20,
                    uploader="account",
                    raw={},
                )
                return SourceAsset(metadata, media_path, metadata_path)

            with (
                patch.dict(os.environ, {"SHORTBRIDGE_HOME": str(home)}),
                patch.object(cli, "download_source", side_effect=fake_download),
            ):
                exit_code = main(
                    [
                        "--json",
                        "schedule",
                        "add",
                        "https://www.instagram.com/reel/reel-1/",
                        "--start",
                        "2099-01-02",
                        "--privacy",
                        "public",
                        "--yes",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

            jobs = JobQueue(home / "queue.sqlite3").list()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_body, "Original description")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "queued")
        self.assertIn("Downloading", stderr.getvalue())

    def test_run_due_without_jobs_is_a_successful_noop(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"SHORTBRIDGE_HOME": temporary_directory}):
                exit_code = main(["--json", "--no-input", "run-due"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "idle"})
        self.assertEqual(stderr.getvalue(), "")

    def test_auth_status_never_prints_secret_values(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            credentials = root / "credentials"
            credentials.mkdir()
            (credentials / "tiktok_client_key").write_text("secret-client-key")
            (credentials / "tiktok_client_secret").write_text("secret-client-secret")
            (credentials / "tiktok_refresh_token").write_text("secret-refresh-token")
            with patch.dict(os.environ, {"SHORTBRIDGE_HOME": str(root)}):
                exit_code = main(["--json", "auth", "status"], stdout=stdout, stderr=stderr)

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertNotIn("secret-", output)
        self.assertTrue(json.loads(output)["credentials_configured"])
        self.assertEqual(stderr.getvalue(), "")

    def test_schedule_cancel_preserves_files_and_reports_state(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            media_path = home / "video.mp4"
            metadata_path = home / "video.info.json"
            media_path.write_bytes(b"video")
            metadata_path.write_text("{}")
            queue = JobQueue(home / "queue.sqlite3")
            queue.add(
                job_id="job-1",
                metadata=SourceMetadata(
                    platform="youtube",
                    source_id="source-1",
                    source_url="https://youtu.be/source-1",
                    title="Title",
                    body="Body",
                    duration_seconds=10,
                    uploader="Channel",
                    raw={},
                ),
                media_path=media_path,
                metadata_path=metadata_path,
                start_date=cli._parse_date("2099-01-02"),
                timezone_name="Europe/Rome",
                privacy_level="PUBLIC_TO_EVERYONE",
            )

            with patch.dict(os.environ, {"SHORTBRIDGE_HOME": str(home)}):
                exit_code = main(
                    ["--json", "schedule", "cancel", "job-1"],
                    stdout=stdout,
                    stderr=stderr,
                )

            cancelled = queue.get("job-1")
            media_preserved = media_path.is_file()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "cancelled")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(media_preserved)
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
