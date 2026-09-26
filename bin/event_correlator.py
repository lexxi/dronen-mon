#!/usr/bin/env python3

import argparse
import json
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


VERSION = "1.1"

BASE_DIR = Path(
    "/opt/dronen-mon"
)

WIFI_EVENT_FILE = (
    BASE_DIR
    / "data"
    / "wifi"
    / "wifi_events.jsonl"
)

SDR_EVENT_FILE = (
    BASE_DIR
    / "data"
    / "sdr"
    / "events"
    / "sdr_events.jsonl"
)

CORRELATION_FILE = (
    BASE_DIR
    / "data"
    / "correlation"
    / "correlation_events.jsonl"
)

DEFAULT_WINDOW_SECONDS = 180

DEFAULT_RETENTION_SECONDS = 600

DEFAULT_MIN_WIFI_RSSI = -75

running = True


def now_iso():
    return (
        datetime.now(
            timezone.utc
        )
        .astimezone()
        .isoformat(
            timespec="seconds"
        )
    )


def stop_handler(
    signum,
    frame,
):
    global running

    running = False


def parse_timestamp(
    value,
):
    if not value:
        return None

    try:
        value = value.replace(
            "Z",
            "+00:00",
        )

        dt = (
            datetime
            .fromisoformat(
                value
            )
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt.timestamp()

    except Exception:
        return None


def event_timestamp(
    event,
):
    for field in (
        "timestamp",
        "last_seen",
        "first_seen",
    ):
        ts = parse_timestamp(
            event.get(field)
        )

        if ts is not None:
            return ts

    return time.time()


def get_int(
    value,
    default=0,
):
    try:
        return int(
            float(value)
        )

    except Exception:
        return default


def get_float(
    value,
    default=0.0,
):
    try:
        return float(
            value
        )

    except Exception:
        return default


def wifi_relevant(
    event,
    min_rssi,
):
    classification = (
        event.get(
            "classification"
        )
        or ""
    ).lower()

    score = get_int(
        event.get(
            "score"
        ),
        0,
    )

    rssi = get_int(
        event.get(
            "rssi_best"
        ),
        -999,
    )

    # Harte lokale Relevanzschwelle.
    if rssi < min_rssi:
        return False

    if classification in (
        "observe",
        "interesting",
        "candidate",
    ):
        return True

    return score >= 3


def sdr_relevant(
    event,
):
    return (
        event.get(
            "event_type"
        )
        == "rf_sweep_activity"
    )


def wifi_identifier(
    event,
):
    return (
        event.get("mac")
        or event.get("bssid")
        or event.get(
            "identifier"
        )
        or "unknown"
    )


def pair_key(
    wifi_event,
    sdr_event,
):
    return (
        wifi_identifier(
            wifi_event
        ),
        sdr_event.get(
            "sweep_id"
        ),
    )


def calculate_score(
    wifi_event,
    sdr_event,
):
    wifi_score = get_int(
        wifi_event.get(
            "score"
        ),
        0,
    )

    rssi = get_int(
        wifi_event.get(
            "rssi_best"
        ),
        -999,
    )

    delta = get_float(
        sdr_event.get(
            "max_peak_delta_db"
        ),
        0,
    )

    score = 0

    # WLAN selbst muss bereits
    # auffällig sein.
    if wifi_score >= 7:
        score += 4

    elif wifi_score >= 5:
        score += 3

    elif wifi_score >= 3:
        score += 2

    # Nähe / Signalstärke WLAN.
    if rssi >= -55:
        score += 3

    elif rssi >= -65:
        score += 2

    elif rssi >= -75:
        score += 1

    # Zeitgleich RF-Aktivität.
    score += 2

    # Stärke des RF-Ereignisses.
    if delta >= 10:
        score += 2

    elif delta >= 8:
        score += 1

    return score


def correlation_class(
    score,
):
    if score >= 9:
        return "high"

    if score >= 6:
        return "medium"

    return "low"


def write_correlation(
    wifi_event,
    sdr_event,
    time_difference,
):
    score = calculate_score(
        wifi_event,
        sdr_event,
    )

    result = {
        "timestamp":
            now_iso(),

        "event_type":
            "wifi_sdr_correlation",

        "classification":
            correlation_class(
                score
            ),

        "correlation_score":
            score,

        "time_difference_seconds":
            round(
                time_difference,
                1,
            ),

        "wifi": {
            "event":
                wifi_event.get(
                    "event"
                ),

            "identifier":
                wifi_identifier(
                    wifi_event
                ),

            "classification":
                wifi_event.get(
                    "classification"
                ),

            "score":
                wifi_event.get(
                    "score"
                ),

            "rssi_best":
                wifi_event.get(
                    "rssi_best"
                ),

            "rssi_range_db":
                wifi_event.get(
                    "rssi_range_db"
                ),

            "channel":
                wifi_event.get(
                    "channel"
                ),

            "manufacturer":
                wifi_event.get(
                    "manufacturer"
                ),

            "ssid":
                wifi_event.get(
                    "ssid"
                ),

            "first_seen":
                wifi_event.get(
                    "first_seen"
                ),

            "last_seen":
                wifi_event.get(
                    "last_seen"
                ),
        },

        "sdr": {
            "sensor":
                sdr_event.get(
                    "sensor"
                ),

            "sweep_id":
                sdr_event.get(
                    "sweep_id"
                ),

            "signal_count":
                sdr_event.get(
                    "signal_count"
                ),

            "strongest_frequency_mhz":
                sdr_event.get(
                    "strongest_frequency_mhz"
                ),

            "max_peak_delta_db":
                sdr_event.get(
                    "max_peak_delta_db"
                ),

            "signals":
                sdr_event.get(
                    "signals",
                    [],
                ),

            "timestamp":
                sdr_event.get(
                    "timestamp"
                ),
        },
    }

    CORRELATION_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with CORRELATION_FILE.open(
        "a",
        encoding="utf-8",
    ) as fh:

        fh.write(
            json.dumps(
                result,
                ensure_ascii=False,
            )
            + "\n"
        )

    print(
        "[CORRELATION] "
        f"class="
        f"{result['classification']} "
        f"score={score} "
        f"wifi="
        f"{result['wifi']['identifier']} "
        f"RSSI="
        f"{result['wifi']['rssi_best']} "
        f"sdr="
        f"{result['sdr']['sensor']} "
        f"signals="
        f"{result['sdr']['signal_count']} "
        f"strongest="
        f"{result['sdr']['strongest_frequency_mhz']}MHz "
        f"Δ="
        f"{result['sdr']['max_peak_delta_db']}dB "
        f"dt="
        f"{time_difference:.1f}s",
        flush=True,
    )


class JsonlFollower:

    def __init__(
        self,
        path,
        from_start=False,
    ):
        self.path = Path(
            path
        )

        self.from_start = (
            from_start
        )

        self.file = None


    def open(self):
        if not self.path.exists():
            return False

        self.file = (
            self.path.open(
                "r",
                encoding="utf-8",
            )
        )

        if not self.from_start:
            self.file.seek(
                0,
                os.SEEK_END,
            )

        return True


    def read_new(self):
        events = []

        if self.file is None:
            if not self.open():
                return events

        while True:
            line = (
                self.file.readline()
            )

            if not line:
                break

            line = line.strip()

            if not line:
                continue

            try:
                events.append(
                    json.loads(
                        line
                    )
                )

            except json.JSONDecodeError:
                continue

        return events


def purge_old(
    queue,
    retention_seconds,
):
    cutoff = (
        time.time()
        - retention_seconds
    )

    while queue:

        ts, _ = queue[0]

        if ts >= cutoff:
            break

        queue.popleft()


def correlate_wifi(
    wifi_event,
    sdr_events,
    seen_pairs,
    window_seconds,
    min_rssi,
):
    if not wifi_relevant(
        wifi_event,
        min_rssi,
    ):
        return

    wifi_ts = event_timestamp(
        wifi_event
    )

    for (
        sdr_ts,
        sdr_event,
    ) in sdr_events:

        diff = abs(
            wifi_ts
            - sdr_ts
        )

        if diff > window_seconds:
            continue

        key = pair_key(
            wifi_event,
            sdr_event,
        )

        if key in seen_pairs:
            continue

        seen_pairs.add(
            key
        )

        write_correlation(
            wifi_event,
            sdr_event,
            diff,
        )


def correlate_sdr(
    sdr_event,
    wifi_events,
    seen_pairs,
    window_seconds,
    min_rssi,
):
    if not sdr_relevant(
        sdr_event
    ):
        return

    sdr_ts = event_timestamp(
        sdr_event
    )

    for (
        wifi_ts,
        wifi_event,
    ) in wifi_events:

        if not wifi_relevant(
            wifi_event,
            min_rssi,
        ):
            continue

        diff = abs(
            wifi_ts
            - sdr_ts
        )

        if diff > window_seconds:
            continue

        key = pair_key(
            wifi_event,
            sdr_event,
        )

        if key in seen_pairs:
            continue

        seen_pairs.add(
            key
        )

        write_correlation(
            wifi_event,
            sdr_event,
            diff,
        )


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_SECONDS,
    )

    parser.add_argument(
        "--retention",
        type=int,
        default=DEFAULT_RETENTION_SECONDS,
    )

    parser.add_argument(
        "--min-wifi-rssi",
        type=int,
        default=DEFAULT_MIN_WIFI_RSSI,
    )

    parser.add_argument(
        "--from-start",
        action="store_true",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    CORRELATION_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    CORRELATION_FILE.touch(
        exist_ok=True,
    )

    wifi_follower = JsonlFollower(
        WIFI_EVENT_FILE,
        from_start=args.from_start,
    )

    sdr_follower = JsonlFollower(
        SDR_EVENT_FILE,
        from_start=args.from_start,
    )

    wifi_events = deque()
    sdr_events = deque()

    seen_pairs = set()

    print(
        f"Event Correlator "
        f"V{VERSION}",
        flush=True,
    )

    print(
        f"WLAN: "
        f"{WIFI_EVENT_FILE}",
        flush=True,
    )

    print(
        f"SDR: "
        f"{SDR_EVENT_FILE}",
        flush=True,
    )

    print(
        f"Fenster: "
        f"±{args.window}s",
        flush=True,
    )

    print(
        "Min WLAN RSSI: "
        f"{args.min_wifi_rssi} dBm",
        flush=True,
    )

    print(
        f"Output: "
        f"{CORRELATION_FILE}",
        flush=True,
    )

    while running:

        new_wifi = (
            wifi_follower
            .read_new()
        )

        new_sdr = (
            sdr_follower
            .read_new()
        )

        for event in new_wifi:

            ts = event_timestamp(
                event
            )

            wifi_events.append(
                (
                    ts,
                    event,
                )
            )

            correlate_wifi(
                event,
                sdr_events,
                seen_pairs,
                args.window,
                args.min_wifi_rssi,
            )

        for event in new_sdr:

            if not sdr_relevant(
                event
            ):
                continue

            ts = event_timestamp(
                event
            )

            sdr_events.append(
                (
                    ts,
                    event,
                )
            )

            correlate_sdr(
                event,
                wifi_events,
                seen_pairs,
                args.window,
                args.min_wifi_rssi,
            )

        purge_old(
            wifi_events,
            args.retention,
        )

        purge_old(
            sdr_events,
            args.retention,
        )

        time.sleep(1)

    print(
        "Event Correlator beendet.",
        flush=True,
    )


if __name__ == "__main__":
    main()
