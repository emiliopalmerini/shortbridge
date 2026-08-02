from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from shortbridge.tiktok import HttpResponse, TikTokClient, TikTokError, TokenProvider


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.requests.append((method, url, headers or {}, body))
        return self.responses.pop(0)


def json_response(document: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(document).encode(), headers={})


class TokenProviderTests(unittest.TestCase):
    def test_refreshes_from_credential_files_and_persists_rotated_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            credentials = root / "credentials"
            state = root / "state"
            credentials.mkdir()
            (credentials / "tiktok_client_key").write_text("client-key\n")
            (credentials / "tiktok_client_secret").write_text("client-secret\n")
            (credentials / "tiktok_refresh_token").write_text("initial-refresh\n")
            transport = FakeTransport(
                [
                    json_response(
                        {
                            "access_token": "access-1",
                            "expires_in": 86400,
                            "refresh_token": "refresh-2",
                            "refresh_expires_in": 31536000,
                            "token_type": "Bearer",
                        }
                    )
                ]
            )
            provider = TokenProvider(credentials, state, transport=transport)

            access_token = provider.access_token(now=datetime(2026, 8, 2, tzinfo=UTC))
            cached_token = provider.access_token(now=datetime(2026, 8, 2, 1, tzinfo=UTC))

            self.assertEqual(access_token, "access-1")
            self.assertEqual(cached_token, "access-1")
            self.assertEqual(len(transport.requests), 1)
            request_body = parse_qs(transport.requests[0][3].decode())
            self.assertEqual(request_body["grant_type"], ["refresh_token"])
            self.assertEqual(request_body["refresh_token"], ["initial-refresh"])
            token_file = state / "tiktok-token.json"
            self.assertEqual(json.loads(token_file.read_text())["refresh_token"], "refresh-2")
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)


class TikTokClientTests(unittest.TestCase):
    def test_publishes_local_mp4_and_waits_for_completion(self) -> None:
        responses = [
            json_response(
                {
                    "data": {
                        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                        "max_video_post_duration_sec": 600,
                    },
                    "error": {"code": "ok", "message": "", "log_id": "creator-log"},
                }
            ),
            json_response(
                {
                    "data": {"publish_id": "publish-1", "upload_url": "https://upload.test/video"},
                    "error": {"code": "ok", "message": "", "log_id": "init-log"},
                }
            ),
            HttpResponse(status=201, body=b"", headers={}),
            json_response(
                {
                    "data": {"status": "PUBLISH_COMPLETE"},
                    "error": {"code": "ok", "message": "", "log_id": "status-log"},
                }
            ),
        ]
        transport = FakeTransport(responses)

        with tempfile.TemporaryDirectory() as temporary_directory:
            video = Path(temporary_directory) / "video.mp4"
            video.write_bytes(b"video bytes")
            client = TikTokClient(lambda: "access-token", transport=transport, sleep=lambda _: None)

            publish_id = client.publish(
                media_path=video,
                caption="Title\n\nBody",
                privacy_level="PUBLIC_TO_EVERYONE",
                duration_seconds=30,
            )

        self.assertEqual(publish_id, "publish-1")
        upload = transport.requests[2]
        self.assertEqual(upload[0], "PUT")
        self.assertEqual(upload[2]["Content-Range"], "bytes 0-10/11")
        self.assertEqual(upload[3], b"video bytes")

    def test_unaudited_client_error_explains_required_action(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    {
                        "data": {},
                        "error": {
                            "code": "unaudited_client_can_only_post_to_private_accounts",
                            "message": "client is unaudited",
                            "log_id": "log-1",
                        },
                    },
                    status=403,
                )
            ]
        )
        client = TikTokClient(lambda: "access-token", transport=transport)

        with self.assertRaisesRegex(TikTokError, "audit"):
            client.creator_info()


if __name__ == "__main__":
    unittest.main()
