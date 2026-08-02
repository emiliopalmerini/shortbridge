# Shortbridge

Shortbridge downloads an owned or authorized YouTube Short or Instagram Reel,
preserves its source text, and queues it for direct publishing to TikTok at
18:00. It is designed to run as a daily systemd job on a personal NixOS server.

TikTok does not expose scheduled posts through its Content Posting API. The
schedule is therefore stored locally in SQLite. Each new item gets the first
free day on or after the date selected with `--start`.

## Requirements

- A TikTok for Developers application with the Content Posting API.
- An approved and audited `video.publish` scope for public posts. Unaudited apps
  cannot publish public content.
- `yt-dlp` and `ffmpeg`. The Nix package includes both in its runtime `PATH`.
- Rights or explicit permission to republish every queued video.

YouTube's official API does not provide downloadable video media. Shortbridge
uses `yt-dlp`, which is an unofficial and potentially fragile integration. It
does not bypass DRM or access controls.

## Usage

Inspect a source without downloading it:

```console
shortbridge inspect 'https://www.youtube.com/shorts/VIDEO_ID'
```

Download and queue a public TikTok post for the first free day from a chosen
date:

```console
shortbridge schedule add URL --start 2026-08-03 --privacy public
```

The interactive confirmation can be supplied explicitly for scripts:

```console
shortbridge --no-input schedule add URL \
  --start 2026-08-03 \
  --privacy public \
  --yes
```

Review the local schedule or request stable JSON:

```console
shortbridge schedule list
shortbridge --json schedule list
```

Publish one due item. The NixOS systemd timer runs this command automatically:

```console
shortbridge --no-input run-due
```

## Preserved Text

For YouTube, Shortbridge stores `title` and `description` separately and builds
the TikTok caption as `title`, a blank line, and `description`. For Instagram it
stores and uses `description`. The original yt-dlp `.info.json` is retained next
to the MP4. Captions over TikTok's 2200 UTF-16 code-unit limit fail clearly and
are never silently truncated.

## State And Credentials

State defaults to `~/Library/Application Support/shortbridge` and can be moved
with `SHORTBRIDGE_HOME`. On NixOS it is `/var/lib/shortbridge`.

Credential file lookup uses `SHORTBRIDGE_CREDENTIALS_DIR`, then systemd's
`CREDENTIALS_DIRECTORY`, then `$SHORTBRIDGE_HOME/credentials`. The directory may
contain these files:

```text
tiktok_client_key
tiktok_client_secret
tiktok_refresh_token
yt_dlp_cookies
```

The cookie file is optional. TikTok credentials are required only for
authorization refresh and publishing. Refreshed tokens are written atomically
to `tiktok-token.json` with mode `0600`; secret values are never accepted as
flags or printed by `auth status`.

## Output Contract

Primary results are written to `stdout`. Progress, warnings, and errors are
written to `stderr`. `--json` produces stable structured output, `--quiet`
suppresses progress, and decoration is not emitted. Exit status is `0` for
success, `1` for an operational failure, `2` for invalid usage, and `130` for
interruption.

Use `shortbridge --help` and subcommand help for the complete interface.
