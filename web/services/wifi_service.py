import csv
from pathlib import Path
from collections import Counter

from .common import BASE_DIR, read_jsonl


WIFI_DIR = BASE_DIR / "data" / "wifi"

CLIENTS_CSV = WIFI_DIR / "clients.csv"
CLIENT_HISTORY_CSV = WIFI_DIR / "client_history.csv"
APS_CSV = WIFI_DIR / "aps.csv"
AP_HISTORY_CSV = WIFI_DIR / "ap_history.csv"
EVENT_JSONL = WIFI_DIR / "wifi_events.jsonl"


def _read_csv(path):
    path = Path(path)

    if not path.exists():
        return []

    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def _to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def get_wifi_clients():
    if CLIENT_HISTORY_CSV.exists():
        rows = _read_csv(CLIENT_HISTORY_CSV)

        for row in rows:
            row["score"] = row.get("max_score") or "0"
            row["classification"] = (
                row.get("last_classification") or "background"
            )
            row["rssi_best"] = row.get("rssi_best_seen") or ""
            row["rssi_range_db"] = ""
            row["frames"] = row.get("seen_count") or "0"
            row["channel"] = row.get("last_channel") or ""
            row["frequency_mhz"] = row.get("last_frequency_mhz") or ""
            row["associated_bssid"] = ""
            row["score_num"] = _to_int(row.get("max_score"))
            row["rssi_num"] = _to_int(row.get("rssi_best_seen"), -999)
            row["frames_num"] = _to_int(row.get("seen_count"))

    else:
        rows = _read_csv(CLIENTS_CSV)

        for row in rows:
            row["score_num"] = _to_int(row.get("score"))
            row["rssi_num"] = _to_int(row.get("rssi_best"), -999)
            row["frames_num"] = _to_int(row.get("frames"))

    rows.sort(
        key=lambda r: (
            r["score_num"],
            r.get("last_seen", ""),
        ),
        reverse=True,
    )

    return rows


def get_wifi_aps():
    history = _read_csv(AP_HISTORY_CSV)

    if history:
        rows = history
    else:
        rows = _read_csv(APS_CSV)

    for row in rows:
        row["seen_count_num"] = _to_int(row.get("seen_count") or row.get("beacons"))
        row["runs_seen_num"] = _to_int(row.get("runs_seen"))
        row["rssi_num"] = _to_int(
            row.get("rssi_best_seen") or row.get("rssi_best"),
            -999,
        )

        baseline_value = str(row.get("baseline", "")).strip().lower()
        row["baseline_bool"] = baseline_value in ("1", "true", "yes", "ja")

    rows.sort(
        key=lambda r: (
            not r["baseline_bool"],
            r.get("last_seen", ""),
        ),
        reverse=True,
    )

    return rows


def get_remote_id_events(limit=100):
    rows = [
        row
        for row in read_jsonl(EVENT_JSONL)
        if row.get("event") == "remote_id_detected"
    ]

    rows.sort(
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return rows[:limit]



def get_top_observe_clients(limit=10):
    """
    Liefert die auffälligsten WLAN-Clients aus der persistenten Historie.

    Observe+ umfasst:
      observe, interesting, candidate

    Sortierung:
      1. Score absteigend
      2. RSSI absteigend
      3. letzte Sichtung absteigend
    """
    rows = [
        row
        for row in get_wifi_clients()
        if row.get("classification") in (
            "observe",
            "interesting",
            "candidate",
        )
    ]

    rows.sort(
        key=lambda row: (
            row.get("score_num", 0),
            row.get("rssi_num", -999),
            row.get("last_seen", ""),
        ),
        reverse=True,
    )

    result = []

    for row in rows[:limit]:
        result.append({
            "mac": row.get("mac", ""),
            "manufacturer": row.get("manufacturer", ""),
            "classification": row.get("classification", ""),
            "score": row.get("score_num", 0),
            "rssi_best": row.get("rssi_best", ""),
            "last_seen": row.get("last_seen", ""),
            "seen_count": row.get("frames_num", 0),
            "channel": row.get("channel", ""),
        })

    return result


def get_wifi_summary():
    clients = get_wifi_clients()
    aps = get_wifi_aps()
    rid = get_remote_id_events(limit=100000)

    classifications = Counter(
        row.get("classification", "unknown")
        for row in clients
    )

    notable = sum(
        classifications.get(name, 0)
        for name in ("observe", "interesting", "candidate")
    )

    return {
        "clients": len(clients),
        "aps": len(aps),
        "notable": notable,
        "background": classifications.get("background", 0),
        "observe": classifications.get("observe", 0),
        "interesting": classifications.get("interesting", 0),
        "candidate": classifications.get("candidate", 0),
        "remote_id": len(rid),
    }
