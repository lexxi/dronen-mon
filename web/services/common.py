from pathlib import Path
import json


BASE_DIR = Path("/opt/dronen-mon")


def read_jsonl(path):
    path = Path(path)

    if not path.exists():
        return []

    rows = []

    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()

                if not line:
                    continue

                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []

    return rows
