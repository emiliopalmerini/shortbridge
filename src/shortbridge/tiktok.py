from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TikTokError(Exception):
    """TikTok authentication or publishing failed."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            return HttpResponse(
                status=exc.code,
                body=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except URLError as exc:
            raise TikTokError(f"Could not reach TikTok: {exc.reason}") from exc
        except OSError as exc:
            raise TikTokError(f"TikTok network request failed: {exc}") from exc


class TokenProvider:
    _TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

    def __init__(
        self,
        credentials_directory: Path,
        state_directory: Path,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.credentials_directory = credentials_directory
        self.state_directory = state_directory
        self.transport = transport or UrllibTransport()
        self.token_path = state_directory / "tiktok-token.json"

    def _credential(self, name: str) -> str:
        path = self.credentials_directory / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise TikTokError(f"Missing TikTok credential file: {path}") from exc
        except OSError as exc:
            raise TikTokError(f"Could not read TikTok credential file {path}: {exc}") from exc
        if not value:
            raise TikTokError(f"TikTok credential file is empty: {path}")
        return value

    def _saved_token(self) -> dict[str, Any]:
        try:
            document = json.loads(self.token_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise TikTokError(f"Could not read saved TikTok token state: {exc}") from exc
        if not isinstance(document, dict):
            raise TikTokError("Saved TikTok token state is not a JSON object")
        return document

    def _save_token(self, document: Mapping[str, Any]) -> None:
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_directory, 0o700)
        temporary_path = self.token_path.with_suffix(".tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as token_file:
                os.chmod(temporary_path, 0o600)
                json.dump(document, token_file, ensure_ascii=True, sort_keys=True)
                token_file.write("\n")
                token_file.flush()
                os.fsync(token_file.fileno())
            temporary_path.replace(self.token_path)
            os.chmod(self.token_path, 0o600)
        except OSError as exc:
            raise TikTokError(f"Could not persist refreshed TikTok tokens: {exc}") from exc

    def access_token(self, *, now: datetime | None = None) -> str:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise TikTokError("Current time must include a timezone")
        current_timestamp = current.timestamp()
        saved = self._saved_token()
        access_token = str(saved.get("access_token") or "")
        expires_at = float(saved.get("access_expires_at") or 0)
        if access_token and expires_at - current_timestamp > 300:
            return access_token

        refresh_token = str(saved.get("refresh_token") or "")
        if not refresh_token:
            refresh_token = self._credential("tiktok_refresh_token")
        form = urlencode(
            {
                "client_key": self._credential("tiktok_client_key"),
                "client_secret": self._credential("tiktok_client_secret"),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode()
        response = self.transport.request(
            "POST",
            self._TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        document = _json_document(response, operation="refresh TikTok authorization")
        if response.status < 200 or response.status >= 300 or document.get("error"):
            detail = document.get("error_description") or document.get("message") or response.status
            raise TikTokError(f"Could not refresh TikTok authorization: {detail}")

        new_access_token = str(document.get("access_token") or "")
        new_refresh_token = str(document.get("refresh_token") or "")
        if not new_access_token or not new_refresh_token:
            raise TikTokError("TikTok token response omitted access_token or refresh_token")
        saved_document = dict(document)
        saved_document["access_expires_at"] = current_timestamp + int(document.get("expires_in") or 0)
        saved_document["refresh_expires_at"] = current_timestamp + int(
            document.get("refresh_expires_in") or 0
        )
        self._save_token(saved_document)
        return new_access_token


def _json_document(response: HttpResponse, *, operation: str) -> dict[str, Any]:
    try:
        document = json.loads(response.body or b"{}")
    except json.JSONDecodeError as exc:
        raise TikTokError(f"TikTok returned invalid JSON while trying to {operation}") from exc
    if not isinstance(document, dict):
        raise TikTokError(f"TikTok returned an unexpected response while trying to {operation}")
    return document


class TikTokClient:
    _API_ROOT = "https://open.tiktokapis.com"

    def __init__(
        self,
        access_token: Callable[[], str],
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.access_token = access_token
        self.transport = transport or UrllibTransport()
        self.sleep = sleep

    def _post(self, path: str, payload: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
        response = self.transport.request(
            "POST",
            f"{self._API_ROOT}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            body=json.dumps(payload, separators=(",", ":")).encode(),
        )
        document = _json_document(response, operation=operation)
        error = document.get("error")
        error_document = error if isinstance(error, dict) else {}
        error_code = str(error_document.get("code") or "")
        if response.status < 200 or response.status >= 300 or error_code not in {"", "ok"}:
            if error_code == "unaudited_client_can_only_post_to_private_accounts":
                raise TikTokError(
                    "TikTok rejected the post because the developer app has not passed its audit"
                )
            message = error_document.get("message") or f"HTTP {response.status}"
            log_id = error_document.get("log_id")
            suffix = f" (TikTok log ID: {log_id})" if log_id else ""
            raise TikTokError(f"Could not {operation}: {error_code or message}: {message}{suffix}")
        data = document.get("data")
        if not isinstance(data, dict):
            raise TikTokError(f"TikTok omitted response data while trying to {operation}")
        return data

    def creator_info(self) -> dict[str, Any]:
        return self._post(
            "/v2/post/publish/creator_info/query/",
            {},
            operation="query TikTok creator settings",
        )

    @staticmethod
    def _chunk_configuration(video_size: int) -> tuple[int, int]:
        if video_size <= 64_000_000:
            return video_size, 1
        chunk_size = 10_000_000
        return chunk_size, math.ceil(video_size / chunk_size)

    def publish(
        self,
        *,
        media_path: Path,
        caption: str,
        privacy_level: str,
        duration_seconds: float | None,
    ) -> str:
        try:
            video_size = media_path.stat().st_size
        except OSError as exc:
            raise TikTokError(f"Could not read scheduled video {media_path}: {exc}") from exc
        if video_size <= 0:
            raise TikTokError(f"Scheduled video is empty: {media_path}")

        creator = self.creator_info()
        privacy_options = creator.get("privacy_level_options")
        if not isinstance(privacy_options, list) or privacy_level not in privacy_options:
            raise TikTokError(
                f"TikTok account does not currently allow privacy level {privacy_level}"
            )
        maximum_duration = creator.get("max_video_post_duration_sec")
        if duration_seconds is not None and isinstance(maximum_duration, (int, float)):
            if duration_seconds > maximum_duration:
                raise TikTokError(
                    f"Video is {duration_seconds:g}s but TikTok currently allows {maximum_duration:g}s"
                )

        chunk_size, total_chunks = self._chunk_configuration(video_size)
        initialized = self._post(
            "/v2/post/publish/video/init/",
            {
                "post_info": {
                    "title": caption,
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "brand_content_toggle": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                },
            },
            operation="initialize TikTok publishing",
        )
        publish_id = str(initialized.get("publish_id") or "")
        upload_url = str(initialized.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise TikTokError("TikTok omitted publish_id or upload_url from the upload response")

        self._upload(media_path, upload_url, video_size, chunk_size)
        self._wait_until_published(publish_id)
        return publish_id

    def _upload(self, media_path: Path, upload_url: str, video_size: int, chunk_size: int) -> None:
        offset = 0
        try:
            with media_path.open("rb") as video:
                while offset < video_size:
                    chunk = video.read(chunk_size)
                    if not chunk:
                        raise TikTokError("Scheduled video ended before its recorded file size")
                    end = offset + len(chunk) - 1
                    response = self.transport.request(
                        "PUT",
                        upload_url,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{video_size}",
                        },
                        body=chunk,
                    )
                    if response.status not in {200, 201, 206}:
                        raise TikTokError(f"TikTok video upload failed with HTTP {response.status}")
                    offset += len(chunk)
        except OSError as exc:
            raise TikTokError(f"Could not stream scheduled video {media_path}: {exc}") from exc

    def _wait_until_published(self, publish_id: str) -> None:
        for attempt in range(150):
            data = self._post(
                "/v2/post/publish/status/fetch/",
                {"publish_id": publish_id},
                operation="fetch TikTok publishing status",
            )
            status = str(data.get("status") or "")
            if status == "PUBLISH_COMPLETE":
                return
            if status == "FAILED":
                reason = data.get("fail_reason") or "TikTok returned no failure reason"
                raise TikTokError(f"TikTok failed to publish {publish_id}: {reason}")
            if attempt < 149:
                self.sleep(2)
        raise TikTokError(
            f"TikTok is still processing publish ID {publish_id}; check its status before retrying"
        )
