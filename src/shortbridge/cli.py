from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TextIO
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import __version__
from .paths import AppPaths
from .queue import Job, JobQueue, QueueError
from .source import SourceError, SourceMetadata, download_source, inspect_source
from .tiktok import TikTokClient, TikTokError, TokenProvider
from .worker import WorkerError, publish_next_due


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, stdout: TextIO, stderr: TextIO, **kwargs: object) -> None:
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str | None, file: TextIO | None = None) -> None:
        if message:
            (self.stderr if file is sys.stderr else self.stdout).write(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            (self.stderr if status else self.stdout).write(message)
        raise _ParserExit(status)

    def error(self, message: str) -> None:
        self.stderr.write(f"Error: {message}.\nRun 'shortbridge --help' for usage.\n")
        raise _ParserExit(2)


def _build_parser(stdout: TextIO, stderr: TextIO) -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="shortbridge",
        description="Schedule owned YouTube Shorts or Instagram videos for TikTok.",
        epilog=(
            "Examples:\n"
            "  shortbridge inspect URL\n"
            "  shortbridge schedule add URL --start 2026-08-03 --privacy public\n"
            "  shortbridge schedule list\n"
            "  shortbridge run-due --no-input\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        stdout=stdout,
        stderr=stderr,
    )
    parser.add_argument("--version", action="version", version=f"shortbridge {__version__}")
    parser.add_argument("--json", action="store_true", help="emit stable JSON output")
    parser.add_argument("--quiet", action="store_true", help="suppress non-error diagnostics")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    parser.add_argument("--no-input", action="store_true", help="never prompt for input")

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    inspect_parser = commands.add_parser(
        "inspect", help="inspect source video and text", stdout=stdout, stderr=stderr
    )
    inspect_parser.add_argument("url", metavar="URL", help="YouTube Short or Instagram Reel URL")

    schedule_parser = commands.add_parser(
        "schedule", help="manage the publishing queue", stdout=stdout, stderr=stderr
    )
    schedule_commands = schedule_parser.add_subparsers(dest="schedule_command", metavar="COMMAND")
    schedule_add = schedule_commands.add_parser(
        "add", help="download and enqueue a source video", stdout=stdout, stderr=stderr
    )
    schedule_add.add_argument("url", metavar="URL", help="YouTube Short or Instagram Reel URL")
    schedule_add.add_argument(
        "--start",
        required=True,
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="first date that may be used",
    )
    schedule_add.add_argument(
        "--timezone",
        default="Europe/Rome",
        metavar="ZONE",
        help="IANA timezone for the 18:00 slot (default: Europe/Rome)",
    )
    schedule_add.add_argument(
        "--privacy",
        required=True,
        choices=("public", "friends", "followers", "private"),
        help="TikTok visibility; must be selected explicitly",
    )
    schedule_add.add_argument(
        "--yes",
        action="store_true",
        help="confirm rights to the content and enqueue without prompting",
    )
    schedule_add.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect the source without downloading or changing the queue",
    )
    schedule_commands.add_parser(
        "list", help="list all scheduled jobs", stdout=stdout, stderr=stderr
    )
    schedule_cancel = schedule_commands.add_parser(
        "cancel", help="cancel a queued or failed job", stdout=stdout, stderr=stderr
    )
    schedule_cancel.add_argument(
        "job_id", metavar="JOB_ID", help="job identifier from schedule list"
    )

    auth_parser = commands.add_parser(
        "auth", help="inspect or refresh TikTok authorization", stdout=stdout, stderr=stderr
    )
    auth_commands = auth_parser.add_subparsers(dest="auth_command", metavar="COMMAND")
    auth_commands.add_parser(
        "status", help="show authorization state without secrets", stdout=stdout, stderr=stderr
    )
    auth_commands.add_parser(
        "refresh", help="refresh and verify TikTok authorization", stdout=stdout, stderr=stderr
    )
    commands.add_parser(
        "run-due", help="publish videos whose time has arrived", stdout=stdout, stderr=stderr
    )
    return parser


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a calendar date such as 2026-08-03") from exc


def _metadata_document(metadata: SourceMetadata) -> dict[str, object]:
    return {
        "platform": metadata.platform,
        "source_id": metadata.source_id,
        "source_url": metadata.source_url,
        "source_title": metadata.title,
        "source_body": metadata.body,
        "tiktok_caption": metadata.tiktok_caption,
        "duration_seconds": metadata.duration_seconds,
        "uploader": metadata.uploader,
    }


def _job_document(job: Job) -> dict[str, object]:
    local_time = job.scheduled_at.astimezone(ZoneInfo(job.timezone_name))
    return {
        "id": job.id,
        "status": job.status,
        "platform": job.platform,
        "source_url": job.source_url,
        "source_title": job.source_title,
        "source_body": job.source_body,
        "tiktok_caption": job.tiktok_caption,
        "scheduled_at": local_time.isoformat(),
        "timezone": job.timezone_name,
        "privacy": job.privacy_level,
        "publish_id": job.publish_id,
        "error": job.last_error,
    }


def _write_document(document: object, *, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        json.dump(document, stdout, ensure_ascii=True, sort_keys=True)
        stdout.write("\n")
        return
    if isinstance(document, dict):
        for key, value in document.items():
            if value is not None:
                stdout.write(f"{key}: {value}\n")
        return
    raise TypeError("Human-readable output requires a mapping")


def _privacy_level(value: str) -> str:
    return {
        "public": "PUBLIC_TO_EVERYONE",
        "friends": "MUTUAL_FOLLOW_FRIENDS",
        "followers": "FOLLOWER_OF_CREATOR",
        "private": "SELF_ONLY",
    }[value]


def _confirm_schedule(*, stdin: TextIO, stderr: TextIO) -> bool:
    stderr.write("Confirm that you own this content or have permission to republish it [y/N]: ")
    stderr.flush()
    return stdin.readline().strip().lower() in {"y", "yes"}


def _cookies_path(paths: AppPaths) -> Path | None:
    path = paths.credentials / "yt_dlp_cookies"
    return path if path.is_file() else None


def _run_inspect(args: argparse.Namespace, *, stdout: TextIO) -> int:
    metadata = inspect_source(args.url, cookies_path=_cookies_path(AppPaths.discover()))
    _write_document(_metadata_document(metadata), as_json=args.json, stdout=stdout)
    return 0


def _run_schedule_add(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not args.yes:
        if args.no_input:
            raise QueueError("--no-input requires --yes to confirm content rights")
        if not _confirm_schedule(stdin=stdin, stderr=stderr):
            raise QueueError("Nothing was scheduled; content rights were not confirmed")

    if args.dry_run:
        metadata = inspect_source(args.url, cookies_path=_cookies_path(AppPaths.discover()))
        document = _metadata_document(metadata)
        document["dry_run"] = True
        _write_document(document, as_json=args.json, stdout=stdout)
        return 0

    paths = AppPaths.discover()
    job_id = str(uuid4())
    if not args.quiet:
        stderr.write(f"Downloading source video and text for job {job_id}...\n")
    download_arguments: dict[str, object] = {"destination": paths.media, "stem": job_id}
    cookies_path = _cookies_path(paths)
    if cookies_path is not None:
        download_arguments["cookies_path"] = cookies_path
    asset = download_source(args.url, **download_arguments)
    queue = JobQueue(paths.database)
    job = queue.add(
        job_id=job_id,
        metadata=asset.metadata,
        media_path=asset.media_path,
        metadata_path=asset.metadata_path,
        start_date=args.start,
        timezone_name=args.timezone,
        privacy_level=_privacy_level(args.privacy),
    )
    _write_document(_job_document(job), as_json=args.json, stdout=stdout)
    if not args.quiet:
        stderr.write("Queued successfully. Run 'shortbridge schedule list' to review it.\n")
    return 0


def _run_schedule_list(args: argparse.Namespace, *, stdout: TextIO) -> int:
    jobs = JobQueue(AppPaths.discover().database).list()
    documents = [_job_document(job) for job in jobs]
    if args.json:
        _write_document(documents, as_json=True, stdout=stdout)
    elif not documents:
        stdout.write("No scheduled jobs.\n")
    else:
        for index, document in enumerate(documents):
            if index:
                stdout.write("\n")
            _write_document(document, as_json=False, stdout=stdout)
    return 0


def _run_schedule_cancel(args: argparse.Namespace, *, stdout: TextIO) -> int:
    queue = JobQueue(AppPaths.discover().database)
    queue.cancel(args.job_id)
    job = queue.get(args.job_id)
    if job is None:
        raise QueueError(f"Job '{args.job_id}' disappeared after cancellation")
    _write_document(_job_document(job), as_json=args.json, stdout=stdout)
    return 0


def _credentials_configured(paths: AppPaths) -> bool:
    names = ("tiktok_client_key", "tiktok_client_secret", "tiktok_refresh_token")
    return all((paths.credentials / name).is_file() for name in names)


def _run_auth_status(args: argparse.Namespace, *, stdout: TextIO) -> int:
    paths = AppPaths.discover()
    document = {
        "credentials_configured": _credentials_configured(paths),
        "refreshed_token_saved": (paths.home / "tiktok-token.json").is_file(),
    }
    _write_document(document, as_json=args.json, stdout=stdout)
    return 0


def _token_provider(paths: AppPaths) -> TokenProvider:
    return TokenProvider(paths.credentials, paths.home)


def _run_auth_refresh(args: argparse.Namespace, *, stdout: TextIO) -> int:
    paths = AppPaths.discover()
    _token_provider(paths).access_token()
    _write_document(
        {"status": "authorized", "credentials_configured": True},
        as_json=args.json,
        stdout=stdout,
    )
    return 0


def _run_due(args: argparse.Namespace, *, stdout: TextIO) -> int:
    paths = AppPaths.discover()
    token_provider = _token_provider(paths)
    client = TikTokClient(token_provider.access_token)
    published = publish_next_due(JobQueue(paths.database), client)
    if published is None:
        _write_document({"status": "idle"}, as_json=args.json, stdout=stdout)
    else:
        _write_document(_job_document(published), as_json=args.json, stdout=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _build_parser(stdout, stderr)

    try:
        args = parser.parse_args(argv)
    except _ParserExit as exc:
        return exc.status

    if args.command is None:
        stderr.write("Choose a command. Run 'shortbridge --help' for examples.\n")
        return 2

    try:
        if args.command == "inspect":
            return _run_inspect(args, stdout=stdout)
        if args.command == "schedule" and args.schedule_command == "add":
            return _run_schedule_add(args, stdin=stdin, stdout=stdout, stderr=stderr)
        if args.command == "schedule" and args.schedule_command == "list":
            return _run_schedule_list(args, stdout=stdout)
        if args.command == "schedule" and args.schedule_command == "cancel":
            return _run_schedule_cancel(args, stdout=stdout)
        if args.command == "schedule":
            stderr.write("Choose a schedule command. Run 'shortbridge schedule --help'.\n")
            return 2
        if args.command == "auth" and args.auth_command == "status":
            return _run_auth_status(args, stdout=stdout)
        if args.command == "auth" and args.auth_command == "refresh":
            return _run_auth_refresh(args, stdout=stdout)
        if args.command == "auth":
            stderr.write("Choose an auth command. Run 'shortbridge auth --help'.\n")
            return 2
        if args.command == "run-due":
            return _run_due(args, stdout=stdout)
        stderr.write(f"Error: command '{args.command}' is not implemented yet.\n")
        return 2
    except (QueueError, SourceError, TikTokError, WorkerError) as exc:
        stderr.write(f"Error: {exc}\n")
        return 1
    except KeyboardInterrupt:
        stderr.write("Cancelled.\n")
        return 130
