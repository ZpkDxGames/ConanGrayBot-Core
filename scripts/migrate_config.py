"""Offline configuration migration with dry-run and private immutable backups."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.migrations import migrate_config  # noqa: E402


def private_destination(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) and not any(
        resolved.is_relative_to(ROOT / folder) for folder in ("private", "secrets")
    ):
        raise ValueError(
            "Migration output must be outside the repository or in its ignored private directory."
        )
    return resolved


def write_private(path: Path, payload: dict[str, Any]) -> None:
    path = private_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".migration-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive atomic publication never overwrites an existing backup.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def migrate_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    if "config" in payload:
        if set(payload) != {"guildId", "config"} or not str(payload["guildId"]).isdigit():
            raise ValueError("Invalid configuration envelope")
        if not isinstance(payload["config"], dict):
            raise ValueError("Invalid configuration envelope")
        return {
            "guildId": str(payload["guildId"]),
            "config": migrate_config(payload["config"]),
        }
    return migrate_config(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Private JSON configuration export")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write migrated snapshot and immutable original backup",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.source.stat().st_size > 256000:
            raise ValueError("Configuration snapshot exceeds 256 KB")
        payload = json.loads(args.source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid configuration snapshot")
        migrated = migrate_snapshot(payload)
        if args.apply:
            if args.output is None or args.backup is None:
                raise ValueError("Apply requires output and backup destinations")
            destinations = [
                private_destination(args.output),
                private_destination(args.backup),
            ]
            if (
                destinations[0] == destinations[1]
                or args.source.resolve() in destinations
                or any(path.exists() for path in destinations)
            ):
                raise ValueError("Destinations must be new, distinct private files")
            write_private(args.backup, payload)
            write_private(args.output, migrated)
        print(
            "Migration applied to private files."
            if args.apply
            else "Dry-run passed; no files or production data changed."
        )
        return 0
    except (OSError, ValueError, TypeError, UnicodeError):
        # Validation errors can contain confidential configuration values.
        print(
            "Migration failed. Check schema, snapshot size and private destination permissions; no configuration contents were printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
