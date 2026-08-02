from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shortbridge.queue import JobQueue, QueueError
from shortbridge.source import SourceMetadata


def youtube_metadata(*, body: str = "Body") -> SourceMetadata:
    return SourceMetadata(
        platform="youtube",
        source_id="source-1",
        source_url="https://youtu.be/source-1",
        title="Title",
        body=body,
        duration_seconds=30,
        uploader="Channel",
        raw={},
    )


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.queue = JobQueue(root / "queue.sqlite3")
        self.media_path = root / "job.mp4"
        self.metadata_path = root / "job.info.json"
        self.media_path.write_bytes(b"video")
        self.metadata_path.write_text("{}")
        self.now = datetime(2026, 8, 2, 12, 0, tzinfo=ZoneInfo("Europe/Rome"))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_add_uses_start_date_at_18_in_rome(self) -> None:
        job = self.queue.add(
            job_id="job-1",
            metadata=youtube_metadata(),
            media_path=self.media_path,
            metadata_path=self.metadata_path,
            start_date=date(2026, 8, 3),
            timezone_name="Europe/Rome",
            privacy_level="PUBLIC_TO_EVERYONE",
            now=self.now,
        )

        self.assertEqual(job.scheduled_at.astimezone(ZoneInfo("Europe/Rome")).isoformat(), "2026-08-03T18:00:00+02:00")
        self.assertEqual(job.source_title, "Title")
        self.assertEqual(job.source_body, "Body")
        self.assertEqual(job.tiktok_caption, "Title\n\nBody")

    def test_add_skips_an_occupied_day(self) -> None:
        arguments = {
            "metadata": youtube_metadata(),
            "media_path": self.media_path,
            "metadata_path": self.metadata_path,
            "start_date": date(2026, 8, 3),
            "timezone_name": "Europe/Rome",
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "now": self.now,
        }
        self.queue.add(job_id="job-1", **arguments)

        second = self.queue.add(job_id="job-2", **arguments)

        self.assertEqual(second.scheduled_at.astimezone(ZoneInfo("Europe/Rome")).date(), date(2026, 8, 4))

    def test_add_does_not_choose_a_time_that_has_passed(self) -> None:
        after_six = datetime(2026, 8, 3, 18, 1, tzinfo=ZoneInfo("Europe/Rome"))

        job = self.queue.add(
            job_id="job-1",
            metadata=youtube_metadata(),
            media_path=self.media_path,
            metadata_path=self.metadata_path,
            start_date=date(2026, 8, 1),
            timezone_name="Europe/Rome",
            privacy_level="SELF_ONLY",
            now=after_six,
        )

        self.assertEqual(job.scheduled_at.astimezone(ZoneInfo("Europe/Rome")).date(), date(2026, 8, 4))

    def test_caption_over_tiktok_limit_is_rejected_without_truncation(self) -> None:
        with self.assertRaisesRegex(QueueError, "2200"):
            self.queue.add(
                job_id="job-1",
                metadata=youtube_metadata(body="x" * 2200),
                media_path=self.media_path,
                metadata_path=self.metadata_path,
                start_date=date(2026, 8, 3),
                timezone_name="Europe/Rome",
                privacy_level="PUBLIC_TO_EVERYONE",
                now=self.now,
            )

        self.assertEqual(self.queue.list(), [])

    def test_claim_due_marks_only_the_oldest_due_job_as_publishing(self) -> None:
        job = self.queue.add(
            job_id="job-1",
            metadata=youtube_metadata(),
            media_path=self.media_path,
            metadata_path=self.metadata_path,
            start_date=date(2026, 8, 3),
            timezone_name="Europe/Rome",
            privacy_level="PUBLIC_TO_EVERYONE",
            now=self.now,
        )

        self.assertIsNone(self.queue.claim_due(now=datetime(2026, 8, 3, 15, 59, tzinfo=ZoneInfo("UTC"))))
        claimed = self.queue.claim_due(now=datetime(2026, 8, 3, 16, 0, tzinfo=ZoneInfo("UTC")))

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "publishing")
        self.assertIsNone(self.queue.claim_due(now=datetime(2026, 8, 3, 16, 1, tzinfo=ZoneInfo("UTC"))))

    def test_failed_job_preserves_actionable_error(self) -> None:
        self.queue.add(
            job_id="job-1",
            metadata=youtube_metadata(),
            media_path=self.media_path,
            metadata_path=self.metadata_path,
            start_date=date(2026, 8, 3),
            timezone_name="Europe/Rome",
            privacy_level="PUBLIC_TO_EVERYONE",
            now=self.now,
        )
        self.queue.claim_due(now=datetime(2026, 8, 3, 16, 0, tzinfo=ZoneInfo("UTC")))

        self.queue.mark_failed("job-1", "TikTok app audit is required")

        failed = self.queue.get("job-1")
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.last_error, "TikTok app audit is required")


if __name__ == "__main__":
    unittest.main()
