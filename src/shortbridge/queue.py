from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .source import SourceMetadata


class QueueError(Exception):
    """A job cannot be added to or read from the local queue."""


@dataclass(frozen=True)
class Job:
    id: str
    status: str
    platform: str
    source_id: str
    source_url: str
    source_title: str | None
    source_body: str
    tiktok_caption: str
    duration_seconds: float | None
    uploader: str | None
    media_path: Path
    metadata_path: Path
    scheduled_at: datetime
    timezone_name: str
    privacy_level: str
    publish_id: str | None
    last_error: str | None


class JobQueue:
    _PRIVACY_LEVELS = {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'publishing', 'published', 'failed', 'cancelled')
                    ),
                    platform TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_title TEXT,
                    source_body TEXT NOT NULL,
                    tiktok_caption TEXT NOT NULL,
                    duration_seconds REAL,
                    uploader TEXT,
                    media_path TEXT NOT NULL,
                    metadata_path TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,
                    privacy_level TEXT NOT NULL,
                    publish_id TEXT,
                    last_error TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_slot
                    ON jobs(scheduled_at_utc)
                    WHERE status != 'cancelled';
                """
            )

    @staticmethod
    def _caption_length(caption: str) -> int:
        return len(caption.encode("utf-16-le")) // 2

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise QueueError("Current time must include a timezone")
        return value.astimezone(UTC)

    def add(
        self,
        *,
        job_id: str,
        metadata: SourceMetadata,
        media_path: Path,
        metadata_path: Path,
        start_date: date,
        timezone_name: str,
        privacy_level: str,
        now: datetime | None = None,
    ) -> Job:
        if not job_id.strip():
            raise QueueError("Job ID cannot be empty")
        if not media_path.is_file() or not metadata_path.is_file():
            raise QueueError("The downloaded video or its source metadata is missing")
        if privacy_level not in self._PRIVACY_LEVELS:
            raise QueueError(f"Unsupported TikTok privacy level: {privacy_level}")
        caption = metadata.tiktok_caption
        if self._caption_length(caption) > 2200:
            raise QueueError("TikTok captions cannot exceed 2200 UTF-16 code units")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise QueueError(f"Unknown timezone: {timezone_name}") from exc

        current = now or datetime.now(UTC)
        current_utc = self._as_utc(current)
        current_local = current_utc.astimezone(timezone)
        candidate_date = max(start_date, current_local.date())
        candidate = datetime.combine(candidate_date, time(18, 0), timezone)
        if candidate <= current_local:
            candidate += timedelta(days=1)

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            while True:
                candidate_utc = candidate.astimezone(UTC)
                occupied = connection.execute(
                    "SELECT 1 FROM jobs WHERE scheduled_at_utc = ? AND status != 'cancelled'",
                    (candidate_utc.isoformat(),),
                ).fetchone()
                if occupied is None:
                    break
                candidate = datetime.combine(
                    candidate.date() + timedelta(days=1),
                    time(18, 0),
                    timezone,
                )

            timestamp = current_utc.isoformat()
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, platform, source_id, source_url, source_title,
                    source_body, tiktok_caption, duration_seconds, uploader,
                    media_path, metadata_path, scheduled_at_utc, timezone_name,
                    privacy_level, created_at_utc, updated_at_utc
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    metadata.platform,
                    metadata.source_id,
                    metadata.source_url,
                    metadata.title,
                    metadata.body,
                    caption,
                    metadata.duration_seconds,
                    metadata.uploader,
                    str(media_path),
                    str(metadata_path),
                    candidate_utc.isoformat(),
                    timezone_name,
                    privacy_level,
                    timestamp,
                    timestamp,
                ),
            )

        job = self.get(job_id)
        if job is None:
            raise QueueError("The job was saved but could not be read back")
        return job

    def get(self, job_id: str) -> Job | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list(self) -> list[Job]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY scheduled_at_utc, created_at_utc"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def claim_due(self, *, now: datetime | None = None) -> Job | None:
        current_utc = self._as_utc(now or datetime.now(UTC))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'queued' AND scheduled_at_utc <= ?
                ORDER BY scheduled_at_utc, created_at_utc
                LIMIT 1
                """,
                (current_utc.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'publishing', updated_at_utc = ?
                WHERE id = ? AND status = 'queued'
                """,
                (current_utc.isoformat(), row["id"]),
            )
            job_id = row["id"]
        return self.get(job_id)

    def mark_failed(self, job_id: str, error: str) -> None:
        message = error.strip()
        if not message:
            raise QueueError("A failed job must include an error message")
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', last_error = ?, updated_at_utc = ?
                WHERE id = ? AND status = 'publishing'
                """,
                (message, timestamp, job_id),
            )
        if cursor.rowcount != 1:
            raise QueueError(f"Job '{job_id}' is not currently publishing")

    def mark_published(self, job_id: str, publish_id: str) -> None:
        if not publish_id.strip():
            raise QueueError("TikTok publish ID cannot be empty")
        timestamp = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'published', publish_id = ?, last_error = NULL, updated_at_utc = ?
                WHERE id = ? AND status = 'publishing'
                """,
                (publish_id, timestamp, job_id),
            )
        if cursor.rowcount != 1:
            raise QueueError(f"Job '{job_id}' is not currently publishing")

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            status=row["status"],
            platform=row["platform"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            source_title=row["source_title"],
            source_body=row["source_body"],
            tiktok_caption=row["tiktok_caption"],
            duration_seconds=row["duration_seconds"],
            uploader=row["uploader"],
            media_path=Path(row["media_path"]),
            metadata_path=Path(row["metadata_path"]),
            scheduled_at=datetime.fromisoformat(row["scheduled_at_utc"]),
            timezone_name=row["timezone_name"],
            privacy_level=row["privacy_level"],
            publish_id=row["publish_id"],
            last_error=row["last_error"],
        )
