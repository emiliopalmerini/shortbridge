from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shortbridge.queue import JobQueue
from shortbridge.source import SourceMetadata
from shortbridge.tiktok import TikTokError
from shortbridge.worker import WorkerError, publish_next_due


class FakePublisher:
    def __init__(self, *, publish_id: str = "publish-1", error: str | None = None) -> None:
        self.publish_id = publish_id
        self.error = error
        self.calls = 0

    def publish(self, **kwargs: object) -> str:
        self.calls += 1
        if self.error:
            raise TikTokError(self.error)
        return self.publish_id


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.media_path = root / "video.mp4"
        self.metadata_path = root / "video.info.json"
        self.media_path.write_bytes(b"video")
        self.metadata_path.write_text("{}")
        self.queue = JobQueue(root / "queue.sqlite3")
        metadata = SourceMetadata(
            platform="youtube",
            source_id="source-1",
            source_url="https://youtu.be/source-1",
            title="Title",
            body="Body",
            duration_seconds=30,
            uploader="Channel",
            raw={},
        )
        self.queue.add(
            job_id="job-1",
            metadata=metadata,
            media_path=self.media_path,
            metadata_path=self.metadata_path,
            start_date=date(2026, 8, 3),
            timezone_name="Europe/Rome",
            privacy_level="PUBLIC_TO_EVERYONE",
            now=datetime(2026, 8, 2, tzinfo=ZoneInfo("Europe/Rome")),
        )
        self.due_time = datetime(2026, 8, 3, 16, 0, tzinfo=ZoneInfo("UTC"))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_publishes_and_marks_due_job(self) -> None:
        publisher = FakePublisher()

        result = publish_next_due(self.queue, publisher, now=self.due_time)

        self.assertIsNotNone(result)
        self.assertEqual(result.id, "job-1")
        published = self.queue.get("job-1")
        self.assertEqual(published.status, "published")
        self.assertEqual(published.publish_id, "publish-1")

    def test_failure_is_persisted_and_reported(self) -> None:
        publisher = FakePublisher(error="app audit required")

        with self.assertRaisesRegex(WorkerError, "app audit required"):
            publish_next_due(self.queue, publisher, now=self.due_time)

        failed = self.queue.get("job-1")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.last_error, "app audit required")

    def test_returns_none_before_a_job_is_due(self) -> None:
        publisher = FakePublisher()

        result = publish_next_due(
            self.queue,
            publisher,
            now=datetime(2026, 8, 3, 15, 59, tzinfo=ZoneInfo("UTC")),
        )

        self.assertIsNone(result)
        self.assertEqual(publisher.calls, 0)


if __name__ == "__main__":
    unittest.main()
