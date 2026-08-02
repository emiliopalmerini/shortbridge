from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    home: Path

    @property
    def database(self) -> Path:
        return self.home / "queue.sqlite3"

    @property
    def media(self) -> Path:
        return self.home / "media"

    @property
    def credentials(self) -> Path:
        override = os.environ.get("SHORTBRIDGE_CREDENTIALS_DIR")
        systemd_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        return Path(override or systemd_directory or self.home / "credentials").expanduser()

    @classmethod
    def discover(cls) -> AppPaths:
        override = os.environ.get("SHORTBRIDGE_HOME")
        if override:
            return cls(Path(override).expanduser())
        return cls(Path.home() / "Library" / "Application Support" / "shortbridge")
