#!/usr/bin/env python3

import argparse
import subprocess
from datetime import datetime
from pathlib import Path


BASE_DIR = Path("/opt/dronen-mon")

CAPTURE_DIR = (
    BASE_DIR
    / "data"
    / "sdr"
    / "captures"
)

LOG_DIR = BASE_DIR / "log"

RUN_LOG = LOG_DIR / "sdr_runs.log"


def timestamp():
    return (
        datetime.now()
        .astimezone()
        .isoformat(timespec="seconds")
    )


def log(message):
    line = f"{timestamp()} {message}"

    print(line, flush=True)

    with RUN_LOG.open(
        "a",
        encoding="utf-8",
    ) as fh:

        fh.write(line + "\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "rtl_power Capture mit "
            "automatischem Start/End-Logging"
        )
    )

    parser.add_argument(
        "--name",
        required=True,
    )

    parser.add_argument(
        "--range",
        required=True,
        dest="frequency_range",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--duration",
        default="1h",
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    return parser.parse_args()


def main():
    args = parse_args()

    CAPTURE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = Path(args.output)

    if not output.is_absolute():
        output = CAPTURE_DIR / output

    command = [
        "rtl_power",
        "-f",
        args.frequency_range,
        "-i",
        str(args.interval),
        "-e",
        args.duration,
        str(output),
    ]

    log(
        f"START "
        f"name={args.name} "
        f"range={args.frequency_range} "
        f"interval={args.interval}s "
        f"duration={args.duration} "
        f"output={output}"
    )

    rc = 1

    try:
        result = subprocess.run(command)
        rc = result.returncode

    except KeyboardInterrupt:
        rc = 130
        log(
            f"ABORT "
            f"name={args.name} "
            f"output={output}"
        )

    except Exception as exc:
        rc = 1

        log(
            f"ERROR "
            f"name={args.name} "
            f"error={exc}"
        )

    finally:
        size = 0

        if output.exists():
            try:
                size = output.stat().st_size
            except OSError:
                pass

        log(
            f"END "
            f"name={args.name} "
            f"returncode={rc} "
            f"bytes={size} "
            f"output={output}"
        )

    raise SystemExit(rc or 0)


if __name__ == "__main__":
    main()
