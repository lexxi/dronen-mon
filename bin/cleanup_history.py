#!/usr/bin/env python3

import argparse
import fcntl
import gzip
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


VERSION = "1.0"

BASE_DIR = Path("/opt/dronen-mon")

SOURCES = [
    {
        "name": "pluto_rf",
        "path": BASE_DIR / "data" / "pluto" / "events" / "pluto_rf.jsonl",
        "retention_days": 14,
    },
    {
        "name": "wifi_channel_activity",
        "path": BASE_DIR / "data" / "wifi" / "channel_activity.jsonl",
        "retention_days": 14,
    },
    {
        "name": "unifi_wireless_clients",
        "path": BASE_DIR / "data" / "unifi" / "wireless_clients.jsonl",
        "retention_days": 30,
    },
]

ARCHIVE_DIR = BASE_DIR / "data" / "archive"
ARCHIVE_RETENTION_DAYS = 30
HISTORY_LOCK_FILE = BASE_DIR / "data" / ".history.lock"


def parse_timestamp(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None


def append_archives(source_name, rows_by_day):
    written = 0

    for day, rows in rows_by_day.items():
        out_dir = ARCHIVE_DIR / source_name
        out_dir.mkdir(parents=True, exist_ok=True)

        archive = out_dir / f"{day}.jsonl.gz"

        with gzip.open(
            archive,
            "at",
            encoding="utf-8",
        ) as fh:
            for line in rows:
                fh.write(line)
                if not line.endswith("\n"):
                    fh.write("\n")
                written += 1

    return written


def prune_archives(now):
    cutoff = (now - timedelta(days=ARCHIVE_RETENTION_DAYS)).date()
    removed = 0

    if not ARCHIVE_DIR.exists():
        return removed

    for path in ARCHIVE_DIR.rglob("*.jsonl.gz"):
        try:
            day = datetime.strptime(
                path.name.split(".jsonl.gz")[0],
                "%Y-%m-%d",
            ).date()
        except ValueError:
            continue

        if day < cutoff:
            path.unlink(missing_ok=True)
            removed += 1

    return removed


def clean_source(source, now, dry_run=False):
    path = source["path"]
    retention_days = source["retention_days"]
    cutoff = now - timedelta(days=retention_days)

    if not path.exists():
        return {
            "name": source["name"],
            "path": str(path),
            "retention_days": retention_days,
            "kept": 0,
            "archived": 0,
            "invalid": 0,
            "missing": True,
        }

    kept_lines = []
    archive_rows = defaultdict(list)
    invalid = 0

    with path.open(
        encoding="utf-8",
        errors="replace",
    ) as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                invalid += 1
                kept_lines.append(line)
                continue

            ts = parse_timestamp(row.get("timestamp"))

            if ts is None:
                invalid += 1
                kept_lines.append(line)
                continue

            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            if ts >= cutoff:
                kept_lines.append(line)
                continue

            day = ts.astimezone().date().isoformat()
            archive_rows[day].append(line)

    archived = sum(
        len(rows)
        for rows in archive_rows.values()
    )

    if not dry_run:
        append_archives(
            source["name"],
            archive_rows,
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        fd, temp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )

        try:
            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
            ) as out:
                for line in kept_lines:
                    out.write(line)
                    if not line.endswith("\n"):
                        out.write("\n")

                out.flush()
                os.fsync(out.fileno())

            try:
                shutil.copymode(
                    path,
                    temp_name,
                )
            except Exception:
                pass

            os.replace(
                temp_name,
                path,
            )

        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    return {
        "name": source["name"],
        "path": str(path),
        "retention_days": retention_days,
        "kept": len(kept_lines),
        "archived": archived,
        "invalid": invalid,
        "missing": False,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Archiviert alte Drohnen-Monitor-Rohdaten "
            "und begrenzt aktive JSONL-Dateien."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur anzeigen, nichts verändern.",
    )
    args = parser.parse_args()

    now = datetime.now().astimezone()

    print(
        "=== Drohnen-Monitor History Cleanup ==="
    )
    print(
        f"Zeit: {now.isoformat(timespec='seconds')}"
    )
    print(
        f"Archive behalten: {ARCHIVE_RETENTION_DAYS} Tage"
    )
    print()

    HISTORY_LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with HISTORY_LOCK_FILE.open("a+") as lock_fh:
        fcntl.flock(
            lock_fh.fileno(),
            fcntl.LOCK_EX,
        )

        for source in SOURCES:
            result = clean_source(
                source,
                now,
                dry_run=args.dry_run,
            )

            if result["missing"]:
                print(
                    f"{result['name']}: fehlt "
                    f"({result['path']})"
                )
                continue

            print(
                f"{result['name']}: "
                f"Retention={result['retention_days']}d "
                f"behalten={result['kept']} "
                f"archiviert={result['archived']} "
                f"ungueltig={result['invalid']}"
            )

        if args.dry_run:
            print()
            print("Dry-Run: keine Dateien verändert.")
        else:
            removed = prune_archives(now)
            print()
            print(
                f"Alte Archive gelöscht: {removed}"
            )

    print()
    print(
        "Nicht verändert: candidates.jsonl, "
        "candidates_current.json"
    )


if __name__ == "__main__":
    main()
