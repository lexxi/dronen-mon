import json
from pathlib import Path
from collections import Counter, defaultdict

from .common import BASE_DIR, read_jsonl


SDR_DIR = BASE_DIR / "data" / "sdr"
EVENT_FILE = SDR_DIR / "events" / "sdr_events.jsonl"
PERSISTENT_DIR = SDR_DIR / "persistent"


def get_sdr_events(limit=100):
    rows = [
        row
        for row in read_jsonl(EVENT_FILE)
        if row.get("event_type") == "rf_sweep_activity"
    ]

    rows.sort(
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return rows[:limit]


def get_persistent_signals():
    result = {}

    if not PERSISTENT_DIR.exists():
        return result

    for path in sorted(PERSISTENT_DIR.glob("persistent_*.json")):
        sensor = path.stem.replace("persistent_", "")

        try:
            with path.open(encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception:
            rows = []

        learned = [
            row
            for row in rows
            if row.get("learned")
        ]

        learned.sort(
            key=lambda r: float(r.get("center_frequency_mhz", 0))
        )

        result[sensor] = learned

    return result


def get_sdr_summary():
    events = get_sdr_events(limit=100000)
    by_sensor = Counter(
        event.get("sensor", "?")
        for event in events
    )

    persistent = get_persistent_signals()

    return {
        "events_total": len(events),
        "events_by_sensor": dict(by_sensor),
        "persistent_total": sum(len(rows) for rows in persistent.values()),
        "persistent_by_sensor": {
            sensor: len(rows)
            for sensor, rows in persistent.items()
        },
    }
