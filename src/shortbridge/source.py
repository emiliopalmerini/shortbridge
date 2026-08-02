from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class SourceError(Exception):
    """A source URL cannot be inspected or downloaded."""


@dataclass(frozen=True)
class SourceMetadata:
    platform: str
    source_id: str
    source_url: str
    title: str | None
    body: str
    duration_seconds: float | None
    uploader: str | None
    raw: Mapping[str, Any]

    @property
    def tiktok_caption(self) -> str:
        if self.platform == "youtube" and self.title:
            return f"{self.title}\n\n{self.body}" if self.body else self.title
        return self.body


@dataclass(frozen=True)
class SourceAsset:
    metadata: SourceMetadata
    media_path: Path
    metadata_path: Path


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _platform_from_document(document: Mapping[str, Any]) -> str:
    extractor = str(document.get("extractor_key") or document.get("extractor") or "").lower()
    if "youtube" in extractor:
        return "youtube"
    if "instagram" in extractor:
        return "instagram"
    raise SourceError("The URL did not resolve to a YouTube or Instagram video")


def metadata_from_document(document: Mapping[str, Any]) -> SourceMetadata:
    platform = _platform_from_document(document)
    source_id = str(document.get("id") or "").strip()
    source_url = str(document.get("webpage_url") or document.get("original_url") or "").strip()
    if not source_id or not source_url:
        raise SourceError("The source metadata is missing its video ID or canonical URL")

    description = str(document.get("description") or "").strip()
    source_title = str(document.get("title") or "").strip()
    title = source_title or None if platform == "youtube" else None
    duration_value = document.get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    uploader_value = str(document.get("uploader") or document.get("channel") or "").strip()

    return SourceMetadata(
        platform=platform,
        source_id=source_id,
        source_url=source_url,
        title=title,
        body=description,
        duration_seconds=duration,
        uploader=uploader_value or None,
        raw=dict(document),
    )


def _validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = {
        "instagram.com",
        "www.instagram.com",
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts:
        raise SourceError("Source must be a YouTube or Instagram HTTP URL")


def inspect_source(
    url: str,
    *,
    cookies_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> SourceMetadata:
    _validate_source_url(url)
    command = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
    ]
    if cookies_path is not None:
        command.extend(("--cookies", str(cookies_path)))
    command.append(url)
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise SourceError("yt-dlp is required but was not found; install it and try again") from exc
    except OSError as exc:
        raise SourceError(f"Could not start yt-dlp: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else "yt-dlp returned no diagnostic"
        raise SourceError(f"Could not inspect the source video: {reason}")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError("yt-dlp returned invalid metadata") from exc
    if not isinstance(document, dict):
        raise SourceError("yt-dlp returned an unexpected metadata document")
    return metadata_from_document(document)


def download_source(
    url: str,
    *,
    destination: Path,
    stem: str,
    cookies_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> SourceAsset:
    _validate_source_url(url)
    if not stem or Path(stem).name != stem:
        raise SourceError("The destination name is invalid")
    destination.mkdir(parents=True, exist_ok=True)
    output_template = destination / f"{stem}.%(ext)s"
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--write-info-json",
        "--format",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "--output",
        str(output_template),
    ]
    if cookies_path is not None:
        command.extend(("--cookies", str(cookies_path)))
    command.append(url)
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise SourceError("yt-dlp is required but was not found; install it and try again") from exc
    except OSError as exc:
        raise SourceError(f"Could not start yt-dlp: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else "yt-dlp returned no diagnostic"
        raise SourceError(f"Could not download the source video: {reason}")

    media_path = destination / f"{stem}.mp4"
    metadata_path = destination / f"{stem}.info.json"
    if not media_path.is_file():
        raise SourceError("yt-dlp completed without producing the expected MP4 video")
    try:
        document = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError("yt-dlp completed without preserving the source metadata") from exc
    except json.JSONDecodeError as exc:
        raise SourceError("yt-dlp saved invalid source metadata") from exc
    if not isinstance(document, dict):
        raise SourceError("yt-dlp saved an unexpected metadata document")

    return SourceAsset(
        metadata=metadata_from_document(document),
        media_path=media_path,
        metadata_path=metadata_path,
    )
