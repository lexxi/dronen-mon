import json
from collections import Counter

from .common import BASE_DIR


CANDIDATE_CURRENT_FILE = (
    BASE_DIR
    / "data"
    / "pluto"
    / "candidates_current.json"
)


def _read_current():
    if not CANDIDATE_CURRENT_FILE.exists():
        return []

    try:
        with CANDIDATE_CURRENT_FILE.open(
            encoding="utf-8",
            errors="replace",
        ) as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def get_candidates(limit=50):
    rows = _read_current()

    rows.sort(
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return rows[:limit]


def get_candidate_summary():
    rows = _read_current()
    counts = Counter(
        row.get("candidate_level", "unknown")
        for row in rows
    )

    return {
        "total": len(rows),
        "candidate": counts.get("candidate", 0),
        "interesting": counts.get("interesting", 0),
        "observe": counts.get("observe", 0),
        "last_timestamp": max(
            (
                row.get("timestamp", "")
                for row in rows
            ),
            default="",
        ),
    }
