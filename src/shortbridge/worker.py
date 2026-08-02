from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .queue import Job, JobQueue, QueueError
from .tiktok import TikTokError


class WorkerError(Exception):
    """A due publishing job could not be completed."""


class Publisher(Protocol):
    def publish(
        self,
        *,
        media_path: Path,
        caption: str,
        privacy_level: str,
        duration_seconds: float | None,
    ) -> str: ...


def publish_next_due(
    queue: JobQueue,
    publisher: Publisher,
    *,
    now: datetime | None = None,
) -> Job | None:
    job = queue.claim_due(now=now)
    if job is None:
        return None
    try:
        publish_id = publisher.publish(
            media_path=job.media_path,
            caption=job.tiktok_caption,
            privacy_level=job.privacy_level,
            duration_seconds=job.duration_seconds,
        )
        queue.mark_published(job.id, publish_id)
    except (TikTokError, QueueError) as exc:
        try:
            queue.mark_failed(job.id, str(exc))
        except QueueError as state_error:
            raise WorkerError(
                f"{exc}; additionally failed to save job state: {state_error}"
            ) from exc
        raise WorkerError(str(exc)) from exc

    published = queue.get(job.id)
    if published is None:
        raise WorkerError(f"Published job '{job.id}' disappeared from the queue")
    return published
