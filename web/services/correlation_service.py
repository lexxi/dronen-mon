from .common import BASE_DIR, read_jsonl


CORRELATION_FILE = (
    BASE_DIR
    / "data"
    / "correlation"
    / "correlation_events.jsonl"
)


def get_correlations(limit=100):
    rows = read_jsonl(CORRELATION_FILE)

    rows.sort(
        key=lambda r: (
            int(r.get("correlation_score", 0) or 0),
            r.get("timestamp", ""),
        ),
        reverse=True,
    )

    return rows[:limit]
