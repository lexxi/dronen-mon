#!/usr/bin/env python3

import csv
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from scapy.all import sniff, Dot11, Dot11Elt, RadioTap

from remote_id_decoder import (
    decode_remote_id_packet,
    build_remote_id_event,
)


VERSION = "2.4"

BASE_DIR = Path("/opt/dronen-mon")
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data" / "wifi"

INTERFACE = os.environ.get("DRONEN_WIFI_INTERFACE", "wlan0")

IGNORE_FILE = CONFIG_DIR / "ignore_bssid.txt"
IGNORE_PREFIX_FILE = CONFIG_DIR / "ignore_prefixes.txt"
KNOWN_FILE = CONFIG_DIR / "known_devices.csv"
UNIFI_CLIENTS_FILE = CONFIG_DIR / "unifi_clients.csv"

BASELINE_AP_CSV = DATA_DIR / "baseline_aps.csv"
AP_HISTORY_CSV = DATA_DIR / "ap_history.csv"

AP_CSV = DATA_DIR / "aps.csv"
CLIENT_CSV = DATA_DIR / "clients.csv"
CLIENT_HISTORY_CSV = DATA_DIR / "client_history.csv"
EVENT_JSONL = DATA_DIR / "wifi_events.jsonl"
CHANNEL_ACTIVITY_JSONL = DATA_DIR / "channel_activity.jsonl"

CHANNELS = [1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161, 165]
CHANNEL_ACTIVITY_INTERVAL = 10

DWELL_SECONDS = 2.0
CSV_INTERVAL = 10

OUI_FILES = [
    Path("/usr/share/ieee-data/oui.txt"),
    Path("/usr/share/misc/oui.txt"),
]

running = True
current_channel = None

aps = {}
clients = {}
client_history = {}

lock = threading.Lock()

ignored_bssids = set()
ignored_prefixes = []
known_devices = {}
unifi_known_devices = {}
UNIFI_RELOAD_SECONDS = 30
baseline_aps = {}
ap_history = {}
run_seen_aps = set()
oui_database = {}

remote_id_seen = {}
REMOTE_ID_EVENT_MIN_INTERVAL = 2.0

channel_activity = {}
channel_visits = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_mac(mac):
    if not mac:
        return None

    return mac.strip().lower()


def run_command(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def is_multicast(mac):
    if not mac:
        return True

    try:
        return bool(int(mac.split(":")[0], 16) & 0x01)
    except Exception:
        return True


def is_locally_administered(mac):
    if not mac:
        return False

    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except Exception:
        return False


def load_ignore_bssids():
    result = set()

    if not IGNORE_FILE.exists():
        return result

    with IGNORE_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lower()

            if not line or line.startswith("#"):
                continue

            result.add(line)

    return result


def load_ignore_prefixes():
    result = []

    if not IGNORE_PREFIX_FILE.exists():
        return result

    with IGNORE_PREFIX_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lower()

            if not line or line.startswith("#"):
                continue

            result.append(line)

    return result


def load_known_devices():
    result = {}

    if not KNOWN_FILE.exists():
        print(f"[WARN] Known-Devices-Datei fehlt: {KNOWN_FILE}")
        return result

    with KNOWN_FILE.open(
        newline="",
        encoding="utf-8",
    ) as fh:

        reader = csv.DictReader(fh)

        for row in reader:
            mac = normalize_mac(row.get("mac"))

            if not mac:
                continue

            result[mac] = {
                "label": (row.get("label") or "").strip(),
                "ip": (row.get("ip") or "").strip(),
                "source": (row.get("source") or "").strip(),
            }

    return result



def load_unifi_clients():
    result = {}

    if not UNIFI_CLIENTS_FILE.exists():
        return result

    try:
        with UNIFI_CLIENTS_FILE.open(
            newline="",
            encoding="utf-8",
        ) as fh:

            reader = csv.DictReader(fh)

            for row in reader:
                mac = normalize_mac(row.get("mac"))

                if not mac:
                    continue

                result[mac] = {
                    "label": (row.get("name") or "").strip(),
                    "ip": (row.get("ip") or "").strip(),
                    "source": "unifi",
                    "connected_at":
                        (row.get("connected_at") or "").strip(),
                    "imported_at":
                        (row.get("imported_at") or "").strip(),
                }

    except Exception as exc:
        print(
            f"[WARN] UniFi-Clients konnten nicht geladen werden: {exc}"
        )

    return result


def unifi_reload_worker():
    global unifi_known_devices

    last_mtime = None

    while running:
        try:
            if UNIFI_CLIENTS_FILE.exists():
                mtime = UNIFI_CLIENTS_FILE.stat().st_mtime

                if last_mtime is None or mtime != last_mtime:
                    fresh = load_unifi_clients()

                    with lock:
                        unifi_known_devices = fresh

                    last_mtime = mtime

                    print(
                        f"[UNIFI] Dynamic Known WLAN Clients: "
                        f"{len(fresh)}"
                    )

        except Exception as exc:
            print(
                f"[WARN] UniFi Known Reload: {exc}"
            )

        time.sleep(UNIFI_RELOAD_SECONDS)


def load_baseline_aps():
    result = {}

    if not BASELINE_AP_CSV.exists():
        print(
            f"[INFO] Noch keine AP-Baseline vorhanden: "
            f"{BASELINE_AP_CSV}"
        )
        return result

    with BASELINE_AP_CSV.open(
        newline="",
        encoding="utf-8",
    ) as fh:

        reader = csv.DictReader(fh)

        for row in reader:
            bssid = normalize_mac(row.get("bssid"))

            if not bssid:
                continue

            result[bssid] = {
                "ssid": (row.get("ssid") or "").strip(),
                "manufacturer":
                    (row.get("manufacturer") or "").strip(),
                "first_seen":
                    (row.get("first_seen") or "").strip(),
                "last_seen":
                    (row.get("last_seen") or "").strip(),
            }

    return result



def load_ap_history():
    result = {}

    if not AP_HISTORY_CSV.exists():
        print(
            f"[INFO] Noch keine persistente AP-Historie vorhanden: "
            f"{AP_HISTORY_CSV}"
        )

        # Beim ersten Start von V1.7 die zuletzt vorhandene aps.csv
        # als Historie übernehmen. So werden APs, die V1.6 bereits
        # gesehen hat, nach dem Upgrade nicht erneut als NEW gemeldet.
        if AP_CSV.exists():
            try:
                with AP_CSV.open(
                    newline="",
                    encoding="utf-8",
                ) as fh:
                    reader = csv.DictReader(fh)

                    for row in reader:
                        bssid = normalize_mac(row.get("bssid"))

                        if not bssid:
                            continue

                        try:
                            seen_count = int(row.get("beacons") or 0)
                        except ValueError:
                            seen_count = 0

                        try:
                            rssi_best_seen = int(row.get("rssi_best"))
                        except (TypeError, ValueError):
                            rssi_best_seen = None

                        result[bssid] = {
                            "bssid": bssid,
                            "ssid": (row.get("ssid") or "").strip(),
                            "manufacturer":
                                (row.get("manufacturer") or "").strip(),
                            "first_seen":
                                (row.get("first_seen") or "").strip(),
                            "last_seen":
                                (row.get("last_seen") or "").strip(),
                            "seen_count": seen_count,
                            "runs_seen": 1,
                            "last_channel":
                                (row.get("channel") or "").strip(),
                            "last_frequency_mhz":
                                (row.get("frequency_mhz") or "").strip(),
                            "rssi_best_seen": rssi_best_seen,
                            "locally_administered":
                                str(
                                    row.get("locally_administered") or ""
                                ).lower() in ("1", "true", "yes"),
                            "baseline":
                                str(row.get("baseline") or "").lower()
                                in ("1", "true", "yes"),
                        }

                if result:
                    print(
                        f"[INFO] AP-Historie aus {AP_CSV} initialisiert: "
                        f"{len(result)} APs"
                    )

            except Exception as exc:
                print(
                    f"[WARN] AP-Historie konnte nicht aus {AP_CSV} "
                    f"initialisiert werden: {exc}"
                )

        return result

    with AP_HISTORY_CSV.open(
        newline="",
        encoding="utf-8",
    ) as fh:

        reader = csv.DictReader(fh)

        for row in reader:
            bssid = normalize_mac(row.get("bssid"))

            if not bssid:
                continue

            try:
                seen_count = int(row.get("seen_count") or 0)
            except ValueError:
                seen_count = 0

            try:
                runs_seen = int(row.get("runs_seen") or 0)
            except ValueError:
                runs_seen = 0

            try:
                rssi_best_seen = int(row.get("rssi_best_seen"))
            except (TypeError, ValueError):
                rssi_best_seen = None

            result[bssid] = {
                "bssid": bssid,
                "ssid": (row.get("ssid") or "").strip(),
                "manufacturer":
                    (row.get("manufacturer") or "").strip(),
                "first_seen":
                    (row.get("first_seen") or "").strip(),
                "last_seen":
                    (row.get("last_seen") or "").strip(),
                "seen_count": seen_count,
                "runs_seen": runs_seen,
                "last_channel":
                    (row.get("last_channel") or "").strip(),
                "last_frequency_mhz":
                    (row.get("last_frequency_mhz") or "").strip(),
                "rssi_best_seen": rssi_best_seen,
                "locally_administered":
                    str(row.get("locally_administered") or "").lower()
                    in ("1", "true", "yes"),
                "baseline":
                    str(row.get("baseline") or "").lower()
                    in ("1", "true", "yes"),
            }

    return result


def update_ap_history(ap, timestamp):
    bssid = ap["bssid"]
    entry = ap_history.get(bssid)

    if entry is None:
        entry = {
            "bssid": bssid,
            "ssid": ap.get("ssid") or "",
            "manufacturer": ap.get("manufacturer") or "",
            "first_seen": timestamp,
            "last_seen": timestamp,
            "seen_count": 0,
            "runs_seen": 0,
            "last_channel": ap.get("channel"),
            "last_frequency_mhz": ap.get("frequency_mhz"),
            "rssi_best_seen": ap.get("rssi_best"),
            "locally_administered":
                bool(ap.get("locally_administered")),
            "baseline": bool(ap.get("baseline")),
        }
        ap_history[bssid] = entry

    if bssid not in run_seen_aps:
        entry["runs_seen"] = int(entry.get("runs_seen") or 0) + 1
        run_seen_aps.add(bssid)

    entry["seen_count"] = int(entry.get("seen_count") or 0) + 1
    entry["last_seen"] = timestamp

    if ap.get("ssid"):
        entry["ssid"] = ap["ssid"]

    if ap.get("manufacturer"):
        entry["manufacturer"] = ap["manufacturer"]

    entry["last_channel"] = ap.get("channel")
    entry["last_frequency_mhz"] = ap.get("frequency_mhz")
    entry["locally_administered"] = bool(
        ap.get("locally_administered")
    )
    entry["baseline"] = bool(ap.get("baseline"))

    rssi = ap.get("rssi_best")
    old_best = entry.get("rssi_best_seen")

    if rssi is not None and (old_best is None or rssi > old_best):
        entry["rssi_best_seen"] = rssi

    ap["history_first_seen"] = entry.get("first_seen")
    ap["history_last_seen"] = entry.get("last_seen")
    ap["history_seen_count"] = entry.get("seen_count", 0)
    ap["history_runs_seen"] = entry.get("runs_seen", 0)

    return entry

def is_known(mac):
    if not mac:
        return False

    mac = normalize_mac(mac)

    return (
        mac in known_devices
        or mac in unifi_known_devices
    )


def is_baseline_ap(mac):
    if not mac:
        return False

    return normalize_mac(mac) in baseline_aps


def is_history_ap(mac):
    if not mac:
        return False

    return normalize_mac(mac) in ap_history


def is_ignored(mac):
    if not mac:
        return False

    mac = normalize_mac(mac)

    if mac in ignored_bssids:
        return True

    for prefix in ignored_prefixes:
        if mac.startswith(prefix):
            return True

    return False


def normalize_oui(mac):
    if not mac:
        return None

    parts = mac.upper().split(":")

    if len(parts) < 3:
        return None

    return "".join(parts[:3])


def load_oui_database():
    database = {}

    selected = None

    for candidate in OUI_FILES:
        if candidate.exists():
            selected = candidate
            break

    if not selected:
        print("[WARN] Keine OUI-Datenbank gefunden.")
        return database

    pattern = re.compile(
        r"^([0-9A-Fa-f]{6})\s+\(base 16\)\s+(.+)$"
    )

    with selected.open(
        encoding="utf-8",
        errors="ignore",
    ) as fh:

        for line in fh:
            match = pattern.match(line.strip())

            if not match:
                continue

            database[match.group(1).upper()] = (
                match.group(2).strip()
            )

    return database


def manufacturer(mac):
    if not mac:
        return None

    if is_locally_administered(mac):
        return None

    return oui_database.get(
        normalize_oui(mac)
    )


def packet_frequency(pkt):
    try:
        if pkt.haslayer(RadioTap):
            value = getattr(
                pkt[RadioTap],
                "ChannelFrequency",
                None,
            )

            if value:
                return int(value)

    except Exception:
        pass

    return None


def frequency_to_channel(freq):
    if not freq:
        return None

    if freq == 2484:
        return 14

    if 2412 <= freq <= 2472:
        return int((freq - 2407) / 5)

    if 5000 <= freq <= 5900:
        return int((freq - 5000) / 5)

    if 5955 <= freq <= 7115:
        return int((freq - 5950) / 5)

    return None


def packet_channel(pkt):
    channel = frequency_to_channel(
        packet_frequency(pkt)
    )

    if channel:
        return channel

    return current_channel


def packet_rssi(pkt):
    try:
        value = int(pkt.dBm_AntSignal)

        if -100 <= value <= -1:
            return value

    except Exception:
        pass

    return None


def frame_bssid(pkt):
    if not pkt.haslayer(Dot11):
        return None

    dot11 = pkt[Dot11]
    bssid = None

    if dot11.type == 0:
        bssid = normalize_mac(dot11.addr3)

    elif dot11.type == 2:
        fc = int(dot11.FCfield)

        to_ds = bool(fc & 0x01)
        from_ds = bool(fc & 0x02)

        if not to_ds and not from_ds:
            bssid = normalize_mac(dot11.addr3)

        elif to_ds and not from_ds:
            bssid = normalize_mac(dot11.addr1)

        elif from_ds and not to_ds:
            bssid = normalize_mac(dot11.addr2)

    if not bssid:
        return None

    if bssid == "ff:ff:ff:ff:ff:ff":
        return None

    if is_multicast(bssid):
        return None

    return bssid


def frame_ssid(pkt):
    if not pkt.haslayer(Dot11):
        return None

    dot11 = pkt[Dot11]

    if dot11.type != 0:
        return None

    if dot11.subtype not in (4, 5, 8):
        return None

    element = pkt.getlayer(Dot11Elt)

    while element:
        try:
            if element.ID == 0:
                if not element.info:
                    return ""

                return element.info.decode(
                    "utf-8",
                    errors="replace",
                )

        except Exception:
            return None

        element = element.payload.getlayer(
            Dot11Elt
        )

    return None


def classification_from_score(score):
    if score >= 7:
        return "candidate"

    if score >= 5:
        return "interesting"

    if score >= 3:
        return "observe"

    return "background"



def save_client_history(history=None):
    if history is None:
        history = client_history

    fields = [
        "mac",
        "manufacturer",
        "first_seen",
        "last_seen",
        "seen_count",
        "runs_seen",
        "last_channel",
        "last_frequency_mhz",
        "rssi_best_seen",
        "max_score",
        "last_classification",
        "locally_administered",
    ]

    temp = Path(str(CLIENT_HISTORY_CSV) + ".tmp")

    rows = sorted(
        history.values(),
        key=lambda row: row.get("last_seen", ""),
        reverse=True,
    )

    with temp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    os.replace(temp, CLIENT_HISTORY_CSV)


def load_client_history():
    result = {}

    source = CLIENT_HISTORY_CSV

    if not source.exists():
        source = CLIENT_CSV

    if not source.exists():
        return result

    try:
        with source.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)

            for row in reader:
                mac = normalize_mac(row.get("mac"))

                if not mac:
                    continue

                if source == CLIENT_HISTORY_CSV:
                    try:
                        seen_count = int(float(row.get("seen_count") or 0))
                    except Exception:
                        seen_count = 0

                    try:
                        runs_seen = int(float(row.get("runs_seen") or 0))
                    except Exception:
                        runs_seen = 0

                    try:
                        max_score = int(float(row.get("max_score") or 0))
                    except Exception:
                        max_score = 0

                    result[mac] = {
                        "mac": mac,
                        "manufacturer": (row.get("manufacturer") or "").strip(),
                        "first_seen": (row.get("first_seen") or "").strip(),
                        "last_seen": (row.get("last_seen") or "").strip(),
                        "seen_count": seen_count,
                        "runs_seen": runs_seen,
                        "last_channel": row.get("last_channel") or "",
                        "last_frequency_mhz": row.get("last_frequency_mhz") or "",
                        "rssi_best_seen": row.get("rssi_best_seen") or "",
                        "max_score": max_score,
                        "last_classification":
                            (row.get("last_classification") or "background").strip(),
                        "locally_administered":
                            (row.get("locally_administered") or "").strip(),
                    }

                else:
                    try:
                        frames = int(float(row.get("frames") or 0))
                    except Exception:
                        frames = 0

                    try:
                        score = int(float(row.get("score") or 0))
                    except Exception:
                        score = 0

                    result[mac] = {
                        "mac": mac,
                        "manufacturer": (row.get("manufacturer") or "").strip(),
                        "first_seen": (row.get("first_seen") or "").strip(),
                        "last_seen": (row.get("last_seen") or "").strip(),
                        "seen_count": frames,
                        "runs_seen": 1,
                        "last_channel": row.get("channel") or "",
                        "last_frequency_mhz": row.get("frequency_mhz") or "",
                        "rssi_best_seen": row.get("rssi_best") or "",
                        "max_score": score,
                        "last_classification":
                            (row.get("classification") or "background").strip(),
                        "locally_administered":
                            (row.get("locally_administered") or "").strip(),
                    }

        if source == CLIENT_CSV and result:
            save_client_history(result)

            print(
                f"[INFO] Client-Historie aus {CLIENT_CSV} "
                f"initialisiert: {len(result)} Clients"
            )

    except Exception as exc:
        print(
            f"[WARN] Client-Historie konnte nicht geladen werden: {exc}"
        )

    return result


def update_client_history(client, new_run=False):
    mac = normalize_mac(client.get("mac"))

    if not mac:
        return

    hist = client_history.get(mac)

    if hist is None:
        hist = {
            "mac": mac,
            "manufacturer": client.get("manufacturer") or "",
            "first_seen": client.get("first_seen") or now_iso(),
            "last_seen": client.get("last_seen") or now_iso(),
            "seen_count": 0,
            "runs_seen": 0,
            "last_channel": "",
            "last_frequency_mhz": "",
            "rssi_best_seen": "",
            "max_score": 0,
            "last_classification": "background",
            "locally_administered":
                client.get("locally_administered", False),
        }

        client_history[mac] = hist

    hist["last_seen"] = client.get("last_seen") or now_iso()
    hist["manufacturer"] = (
        client.get("manufacturer")
        or hist.get("manufacturer")
        or ""
    )
    hist["seen_count"] = int(hist.get("seen_count") or 0) + 1

    if new_run:
        hist["runs_seen"] = int(hist.get("runs_seen") or 0) + 1

    hist["last_channel"] = client.get("channel") or ""
    hist["last_frequency_mhz"] = client.get("frequency_mhz") or ""

    current_best = client.get("rssi_best")

    try:
        old_best = int(float(hist.get("rssi_best_seen")))
    except Exception:
        old_best = None

    if current_best is not None:
        if old_best is None or current_best > old_best:
            hist["rssi_best_seen"] = current_best

    score = int(client.get("score") or 0)
    hist["max_score"] = max(int(hist.get("max_score") or 0), score)

    hist["last_classification"] = (
        client.get("classification")
        or hist.get("last_classification")
        or "background"
    )

    hist["locally_administered"] = client.get(
        "locally_administered",
        hist.get("locally_administered", False),
    )


def calculate_client_score(client):
    score = 0

    rssi_best = client.get("rssi_best")
    rssi_range = client.get(
        "rssi_range_db"
    ) or 0

    frames = client.get("frames") or 0

    duration = client.get(
        "seen_duration_seconds"
    ) or 0

    randomized = client.get(
        "locally_administered",
        False,
    )

    # Starkes Signal
    if rssi_best is not None:
        if rssi_best >= -60:
            score += 2

        elif rssi_best >= -70:
            score += 1

    # Nicht randomisierte MAC:
    # stabiler identifizierbar.
    if not randomized:
        score += 2

    # Wiederholt beobachtet.
    if frames >= 20:
        score += 1

    # Deutliche RSSI-Veränderung.
    if rssi_range >= 15:
        score += 2

    elif rssi_range >= 10:
        score += 1

    # Kurzer, aber mehrfach
    # beobachteter Teilnehmer.
    if (
        15 <= duration <= 180
        and frames >= 5
    ):
        score += 1

    # Sehr schwache Teilnehmer dürfen nicht allein durch
    # Frame-Zahl oder RSSI-Schwankungen hochgestuft werden.
    if rssi_best is not None:
        if rssi_best < -80:
            score = min(score, 2)

        elif rssi_best < -75:
            score = min(score, 4)

    return score


def calculate_ap_score(ap):
    # Baseline-APs bekommen keinen
    # Anomalie-Score.
    if ap.get("baseline"):
        return 0

    score = 0

    rssi_best = ap.get("rssi_best")

    rssi_range = ap.get(
        "rssi_range_db"
    ) or 0

    beacons = ap.get("beacons") or 0

    ssid = ap.get("ssid")

    randomized = ap.get(
        "locally_administered",
        False,
    )

    if rssi_best is not None:
        if rssi_best >= -60:
            score += 2

        elif rssi_best >= -70:
            score += 1

    if randomized:
        score += 1

    # Hidden SSID
    if ssid == "":
        score += 1

    if rssi_range >= 15:
        score += 2

    elif rssi_range >= 10:
        score += 1

    if beacons >= 20:
        score += 1

    return score


def update_rssi_stats(entry, rssi):
    if rssi is None:
        return

    entry["rssi_last"] = rssi

    if (
        entry.get("rssi_best") is None
        or rssi > entry["rssi_best"]
    ):
        entry["rssi_best"] = rssi

    if (
        entry.get("rssi_min") is None
        or rssi < entry["rssi_min"]
    ):
        entry["rssi_min"] = rssi

    if (
        entry.get("rssi_max") is None
        or rssi > entry["rssi_max"]
    ):
        entry["rssi_max"] = rssi

    if (
        entry.get("rssi_min") is not None
        and entry.get("rssi_max") is not None
    ):
        entry["rssi_range_db"] = (
            entry["rssi_max"]
            - entry["rssi_min"]
        )


def update_duration(entry):
    first = entry.get("_first_seen_epoch")

    if first is None:
        return

    entry["seen_duration_seconds"] = round(
        time.time() - first,
        1,
    )


def public_entry(entry):
    return {
        key: value
        for key, value in entry.items()
        if not key.startswith("_")
    }


def write_event(event):
    with EVENT_JSONL.open(
        "a",
        encoding="utf-8",
    ) as fh:

        fh.write(
            json.dumps(
                event,
                ensure_ascii=False,
            )
            + "\n"
        )


def check_classification_change(
    kind,
    identifier,
    entry,
):
    # Baseline ist eigener Zustand.
    if entry.get("baseline"):
        return

    previous = entry.get(
        "_last_event_classification"
    )

    current = entry["classification"]

    if previous == current:
        return

    entry[
        "_last_event_classification"
    ] = current

    # Rückfall auf Background
    # muss momentan nicht als Event
    # protokolliert werden.
    if current == "background":
        return

    event = {
        "event":
            f"{kind}_classification_changed",
        "identifier": identifier,
        **public_entry(entry),
    }

    write_event(event)

    print(
        f"[{kind.upper()} "
        f"{current.upper()}] "
        f"{identifier} "
        f"score={entry['score']} "
        f"RSSI={entry.get('rssi_best')} "
        f"ΔRSSI="
        f"{entry.get('rssi_range_db')}"
    )


def process_ap(pkt):
    dot11 = pkt[Dot11]

    # Nur Beacon und Probe Response.
    if dot11.type != 0:
        return

    if dot11.subtype not in (5, 8):
        return

    bssid = frame_bssid(pkt)

    if not bssid:
        return

    if is_ignored(bssid):
        return

    if is_known(bssid):
        return

    ssid = frame_ssid(pkt)
    rssi = packet_rssi(pkt)
    channel = packet_channel(pkt)
    freq = packet_frequency(pkt)

    timestamp = now_iso()
    epoch = time.time()

    with lock:
        if bssid not in aps:
            baseline = is_baseline_ap(
                bssid
            )

            history_entry = ap_history.get(bssid)
            seen_before = history_entry is not None

            aps[bssid] = {
                "bssid": bssid,
                "ssid": ssid,
                "manufacturer":
                    manufacturer(bssid),

                "first_seen": timestamp,
                "last_seen": timestamp,

                "channel": channel,
                "frequency_mhz": freq,

                "rssi_last": rssi,
                "rssi_best": rssi,
                "rssi_min": rssi,
                "rssi_max": rssi,
                "rssi_range_db": 0,

                "beacons": 1,

                "seen_duration_seconds": 0,

                "locally_administered":
                    is_locally_administered(
                        bssid
                    ),

                "baseline": baseline,

                "history_status":
                    (
                        "baseline"
                        if baseline
                        else (
                            "seen_before"
                            if seen_before
                            else "new"
                        )
                    ),
                "history_first_seen":
                    (
                        history_entry.get("first_seen")
                        if history_entry
                        else timestamp
                    ),
                "history_last_seen": timestamp,
                "history_seen_count":
                    (
                        history_entry.get("seen_count", 0)
                        if history_entry
                        else 0
                    ),
                "history_runs_seen":
                    (
                        history_entry.get("runs_seen", 0)
                        if history_entry
                        else 0
                    ),

                "score": 0,
                "classification":
                    (
                        "baseline"
                        if baseline
                        else "background"
                    ),

                "_first_seen_epoch": epoch,

                "_last_event_classification":
                    (
                        "baseline"
                        if baseline
                        else "background"
                    ),
            }

            ap = aps[bssid]
            update_ap_history(ap, timestamp)

            if not baseline:
                ap["score"] = (
                    calculate_ap_score(ap)
                )

                ap["classification"] = (
                    classification_from_score(
                        ap["score"]
                    )
                )

                ap[
                    "_last_event_classification"
                ] = ap["classification"]

                if not seen_before:
                    write_event({
                        "event": "new_ap",
                        **public_entry(ap),
                    })

                    print(
                        f"[NEW AP] "
                        f"{bssid} "
                        f"SSID={ssid!r} "
                        f"CH={channel} "
                        f"RSSI={rssi} "
                        f"SCORE={ap['score']} "
                        f"CLASS="
                        f"{ap['classification']} "
                        f"VENDOR="
                        f"{manufacturer(bssid)}"
                    )

                else:
                    print(
                        f"[SEEN AP] "
                        f"{bssid} "
                        f"SSID={ssid!r} "
                        f"CH={channel} "
                        f"RSSI={rssi} "
                        f"RUNS="
                        f"{ap['history_runs_seen']} "
                        f"FIRST="
                        f"{ap['history_first_seen']}"
                    )

            else:
                # Baseline-AP nur einmal
                # pro Programmlauf anzeigen.
                print(
                    f"[BASELINE AP] "
                    f"{bssid} "
                    f"SSID={ssid!r} "
                    f"CH={channel} "
                    f"RSSI={rssi}"
                )

        else:
            ap = aps[bssid]

            ap["last_seen"] = timestamp
            ap["channel"] = channel
            ap["frequency_mhz"] = freq
            ap["beacons"] += 1

            if ssid:
                ap["ssid"] = ssid

            update_rssi_stats(
                ap,
                rssi,
            )

            update_duration(ap)
            update_ap_history(ap, timestamp)

            if ap.get("baseline"):
                ap["score"] = 0
                ap["classification"] = (
                    "baseline"
                )

            else:
                ap["score"] = (
                    calculate_ap_score(ap)
                )

                ap["classification"] = (
                    classification_from_score(
                        ap["score"]
                    )
                )

                check_classification_change(
                    "ap",
                    bssid,
                    ap,
                )


def process_client(pkt):
    dot11 = pkt[Dot11]

    # Sender-MAC.
    mac = normalize_mac(
        dot11.addr2
    )

    if not mac:
        return

    if mac == "ff:ff:ff:ff:ff:ff":
        return

    if is_multicast(mac):
        return

    # Eigene bekannte Geräte
    # vollständig ignorieren.
    if is_known(mac):
        return

    if is_ignored(mac):
        return

    # Bereits bekannte AP-BSSIDs nicht nochmals
    # als Clients aufnehmen. Das verhindert
    # False Positives direkt nach einem Neustart,
    # bevor der erste Beacon des APs verarbeitet wurde.
    if is_baseline_ap(mac):
        return

    if is_history_ap(mac):
        return

    bssid = frame_bssid(pkt)

    # Frames zu eigener Infrastruktur
    # ignorieren.
    if bssid and is_ignored(bssid):
        return

    if bssid and is_known(bssid):
        return

    # APs nicht nochmals als
    # Client aufnehmen.
    if mac in aps:
        return

    timestamp = now_iso()
    epoch = time.time()

    rssi = packet_rssi(pkt)
    channel = packet_channel(pkt)
    freq = packet_frequency(pkt)

    randomized = (
        is_locally_administered(mac)
    )

    with lock:
        if mac not in clients:
            clients[mac] = {
                "mac": mac,

                "manufacturer":
                    manufacturer(mac),

                "associated_bssid":
                    bssid,

                "first_seen": timestamp,
                "last_seen": timestamp,

                "channel": channel,
                "frequency_mhz": freq,

                "rssi_last": rssi,
                "rssi_best": rssi,
                "rssi_min": rssi,
                "rssi_max": rssi,
                "rssi_range_db": 0,

                "frames": 1,

                "seen_duration_seconds":
                    0,

                "locally_administered":
                    randomized,

                "score": 0,

                "classification":
                    "background",

                "_first_seen_epoch":
                    epoch,

                "_last_event_classification":
                    "background",
            }

            client = clients[mac]

            client["score"] = (
                calculate_client_score(
                    client
                )
            )

            client["classification"] = (
                classification_from_score(
                    client["score"]
                )
            )

            client[
                "_last_event_classification"
            ] = client["classification"]

            update_client_history(
                client,
                new_run=True,
            )

            write_event({
                "event": "new_client",
                **public_entry(client),
            })

            marker = (
                "RANDOM"
                if randomized
                else "CLIENT"
            )

            print(
                f"[NEW {marker}] "
                f"{mac} "
                f"BSSID={bssid} "
                f"CH={channel} "
                f"RSSI={rssi} "
                f"SCORE="
                f"{client['score']} "
                f"CLASS="
                f"{client['classification']} "
                f"VENDOR="
                f"{manufacturer(mac)}"
            )

        else:
            client = clients[mac]

            client["last_seen"] = (
                timestamp
            )

            client["channel"] = channel
            client[
                "frequency_mhz"
            ] = freq

            client["frames"] += 1

            if bssid:
                client[
                    "associated_bssid"
                ] = bssid

            update_rssi_stats(
                client,
                rssi,
            )

            update_duration(
                client
            )

            client["score"] = (
                calculate_client_score(
                    client
                )
            )

            client["classification"] = (
                classification_from_score(
                    client["score"]
                )
            )

            update_client_history(
                client,
                new_run=False,
            )

            check_classification_change(
                "client",
                mac,
                client,
            )



def process_remote_id(pkt):
    """
    Passive Remote-ID detection.

    The normal AP/client logic remains unchanged. Remote ID is handled
    as an additional protocol indicator from Vendor Specific IEs.
    """
    try:
        detections = decode_remote_id_packet(pkt)
    except Exception as exc:
        print(
            f"[WARN] Remote-ID Decoder: {exc}"
        )
        return

    if not detections:
        return

    timestamp = now_iso()
    epoch = time.time()

    bssid = frame_bssid(pkt)
    transmitter = normalize_mac(
        getattr(pkt[Dot11], "addr2", None)
    )

    rssi = packet_rssi(pkt)
    channel = packet_channel(pkt)
    freq = packet_frequency(pkt)

    for detection in detections:
        # Deduplicate very frequent RID beacons. Keep protocol/message
        # variation separate so Location / Basic ID / System can all appear.
        uas_id = detection.get("uas_id") or ""
        msg_name = detection.get("message_type_name") or ""
        oui = detection.get("oui") or ""

        dedup_key = (
            transmitter or bssid or "?",
            oui,
            msg_name,
            uas_id,
        )

        previous = remote_id_seen.get(dedup_key)

        if (
            previous is not None
            and epoch - previous
            < REMOTE_ID_EVENT_MIN_INTERVAL
        ):
            continue

        remote_id_seen[dedup_key] = epoch

        event = build_remote_id_event(
            detection,
            timestamp=timestamp,
            bssid=bssid,
            transmitter=transmitter,
            rssi=rssi,
            channel=channel,
            frequency_mhz=freq,
        )

        write_event(event)

        rid_id = (
            detection.get("uas_id")
            or detection.get("operator_id")
            or "unknown"
        )

        location = ""

        if (
            detection.get("latitude") is not None
            and detection.get("longitude") is not None
        ):
            location = (
                f" POS={detection['latitude']},"
                f"{detection['longitude']}"
            )

        print(
            "[REMOTE ID] "
            f"TX={transmitter} "
            f"BSSID={bssid} "
            f"OUI={detection.get('oui')} "
            f"TYPE={detection.get('message_type_name')} "
            f"ID={rid_id} "
            f"RSSI={rssi} "
            f"CH={channel} "
            f"DECODE={'OK' if detection.get('decode_ok') else 'PARTIAL'}"
            f"{location}"
        )



def update_channel_activity(pkt):
    """
    Count all observed 802.11 frames per channel.

    This runs independently from AP/client filtering so that known devices,
    baseline APs and ordinary WLAN traffic still contribute to channel load.
    """
    if not pkt.haslayer(Dot11):
        return

    channel = packet_channel(pkt)

    if channel is None:
        return

    freq = packet_frequency(pkt)
    rssi = packet_rssi(pkt)
    dot11 = pkt[Dot11]

    frame_type = int(dot11.type)

    transmitter = normalize_mac(
        getattr(dot11, "addr2", None)
    )

    now = time.time()

    with lock:
        entry = channel_activity.get(channel)

        if entry is None:
            entry = {
                "channel": channel,
                "frequency_mhz": freq,
                "frames_total": 0,
                "management_frames": 0,
                "control_frames": 0,
                "data_frames": 0,
                "unique_macs": set(),
                "strongest_rssi": None,
                "first_epoch": now,
                "last_epoch": now,
            }
            channel_activity[channel] = entry

        entry["frames_total"] += 1
        entry["last_epoch"] = now

        if freq:
            entry["frequency_mhz"] = freq

        if frame_type == 0:
            entry["management_frames"] += 1
        elif frame_type == 1:
            entry["control_frames"] += 1
        elif frame_type == 2:
            entry["data_frames"] += 1

        if transmitter and not is_multicast(transmitter):
            entry["unique_macs"].add(transmitter)

        if rssi is not None:
            strongest = entry.get("strongest_rssi")

            if strongest is None or rssi > strongest:
                entry["strongest_rssi"] = rssi


def write_channel_activity_snapshot():
    timestamp = now_iso()

    with lock:
        snapshot = []

        channels = sorted(
            set(channel_activity)
            | set(channel_visits)
        )

        for channel in channels:
            entry = channel_activity.get(
                channel,
                {}
            )

            visit = channel_visits.get(
                channel,
                {}
            )

            snapshot.append({
                "timestamp": timestamp,
                "window_seconds": CHANNEL_ACTIVITY_INTERVAL,
                "channel": channel,
                "frequency_mhz":
                    entry.get("frequency_mhz")
                    or visit.get("frequency_mhz"),
                "frames_total": entry.get("frames_total", 0),
                "management_frames": entry.get("management_frames", 0),
                "control_frames": entry.get("control_frames", 0),
                "data_frames": entry.get("data_frames", 0),
                "unique_macs": len(entry.get("unique_macs", set())),
                "strongest_rssi": entry.get("strongest_rssi"),
                "channel_visited": True,
            })

        channel_activity.clear()
        channel_visits.clear()

    if not snapshot:
        return

    with CHANNEL_ACTIVITY_JSONL.open(
        "a",
        encoding="utf-8",
    ) as fh:
        for row in snapshot:
            fh.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                )
                + "\n"
            )


def channel_activity_writer():
    while running:
        time.sleep(
            CHANNEL_ACTIVITY_INTERVAL
        )

        try:
            write_channel_activity_snapshot()

        except Exception as exc:
            print(
                f"[WARN] Channel-Activity Writer: {exc}"
            )


def process_packet(pkt):
    if not pkt.haslayer(Dot11):
        return

    # Count all observed WLAN frames before AP/client filters are applied.
    update_channel_activity(pkt)

    # Remote ID is an additional passive protocol detector and does not
    # modify the existing AP/client classification path.
    process_remote_id(pkt)

    process_ap(pkt)
    process_client(pkt)


def channel_hopper():
    global current_channel

    while running:
        for channel in CHANNELS:
            if not running:
                return

            result = run_command([
                "iw",
                "dev",
                INTERFACE,
                "set",
                "channel",
                str(channel),
            ])

            if result.returncode == 0:
                current_channel = channel

                if 1 <= channel <= 13:
                    frequency_mhz = (
                        2407 + 5 * channel
                    )
                elif channel == 14:
                    frequency_mhz = 2484
                else:
                    frequency_mhz = (
                        5000 + 5 * channel
                    )

                now = time.time()

                with lock:
                    visit = channel_visits.get(
                        channel
                    )

                    if visit is None:
                        channel_visits[channel] = {
                            "frequency_mhz":
                                frequency_mhz,
                            "first_epoch": now,
                            "last_epoch": now,
                        }
                    else:
                        visit["last_epoch"] = now

            else:
                print(
                    f"[WARN] "
                    f"CH {channel}: "
                    f"{result.stderr.strip()}"
                )

            time.sleep(
                DWELL_SECONDS
            )


def write_csv_file(
    filename,
    rows,
    fields,
):
    temp = Path(
        str(filename) + ".tmp"
    )

    with temp.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as fh:

        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)

    os.replace(
        temp,
        filename,
    )


def csv_writer():
    ap_fields = [
        "bssid",
        "ssid",
        "manufacturer",

        "first_seen",
        "last_seen",

        "channel",
        "frequency_mhz",

        "rssi_last",
        "rssi_best",
        "rssi_min",
        "rssi_max",
        "rssi_range_db",

        "beacons",
        "seen_duration_seconds",

        "locally_administered",

        "baseline",

        "history_status",
        "history_first_seen",
        "history_last_seen",
        "history_seen_count",
        "history_runs_seen",

        "score",
        "classification",
    ]

    client_fields = [
        "mac",
        "manufacturer",
        "associated_bssid",

        "first_seen",
        "last_seen",

        "channel",
        "frequency_mhz",

        "rssi_last",
        "rssi_best",
        "rssi_min",
        "rssi_max",
        "rssi_range_db",

        "frames",
        "seen_duration_seconds",

        "locally_administered",

        "score",
        "classification",
    ]

    history_fields = [
        "bssid",
        "ssid",
        "manufacturer",
        "first_seen",
        "last_seen",
        "seen_count",
        "runs_seen",
        "last_channel",
        "last_frequency_mhz",
        "rssi_best_seen",
        "locally_administered",
        "baseline",
    ]

    while running:
        time.sleep(
            CSV_INTERVAL
        )

        with lock:
            ap_rows = [
                public_entry(x)
                for x in aps.values()
            ]

            client_rows = [
                public_entry(x)
                for x in clients.values()
            ]

            history_rows = [
                dict(x)
                for x in ap_history.values()
            ]

        write_csv_file(
            AP_CSV,
            ap_rows,
            ap_fields,
        )

        write_csv_file(
            CLIENT_CSV,
            client_rows,
            client_fields,
        )

        with lock:
            save_client_history()

        write_csv_file(
            AP_HISTORY_CSV,
            history_rows,
            history_fields,
        )


def prepare_interface():
    print(
        f"Bereite Interface "
        f"{INTERFACE} vor ..."
    )

    run_command([
        "rfkill",
        "unblock",
        "wifi",
    ])

    time.sleep(0.5)

    result = run_command([
        "ip",
        "link",
        "show",
        INTERFACE,
    ])

    if result.returncode != 0:
        raise RuntimeError(
            f"{INTERFACE} nicht gefunden."
        )

    run_command([
        "ip",
        "link",
        "set",
        INTERFACE,
        "down",
    ])

    result = run_command([
        "iw",
        "dev",
        INTERFACE,
        "set",
        "type",
        "monitor",
    ])

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
        )

    result = run_command([
        "ip",
        "link",
        "set",
        INTERFACE,
        "up",
    ])

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
        )

    time.sleep(1)

    result = run_command([
        "iw",
        "dev",
        INTERFACE,
        "info",
    ])

    if "type monitor" not in result.stdout:
        raise RuntimeError(
            "Monitor Mode nicht aktiv."
        )

    print(
        "Interface ist UP "
        "und im Monitor Mode."
    )


def stop_handler(
    signum,
    frame,
):
    global running

    print(
        "\nBeende WLAN-Monitor ..."
    )

    running = False


def main():
    global ignored_bssids
    global ignored_prefixes
    global known_devices
    global unifi_known_devices
    global baseline_aps
    global ap_history
    global client_history
    global oui_database

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    ignored_bssids = (
        load_ignore_bssids()
    )

    ignored_prefixes = (
        load_ignore_prefixes()
    )

    known_devices = (
        load_known_devices()
    )

    unifi_known_devices = (
        load_unifi_clients()
    )

    baseline_aps = (
        load_baseline_aps()
    )

    ap_history = (
        load_ap_history()
    )

    client_history = (
        load_client_history()
    )

    oui_database = (
        load_oui_database()
    )

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    try:
        prepare_interface()

    except Exception as exc:
        print(
            f"FEHLER: {exc}"
        )

        raise SystemExit(1)

    print()
    print(
        f"WLAN-Monitor V{VERSION}"
    )

    print(
        f"Interface: {INTERFACE}"
    )

    print(
        f"Ignorierte BSSIDs: "
        f"{len(ignored_bssids)}"
    )

    print(
        f"Ignorierte Präfixe: "
        f"{len(ignored_prefixes)}"
    )

    print(
        f"Eigene bekannte MACs: "
        f"{len(known_devices)}"
    )

    print(
        f"UniFi bekannte WLAN-MACs: "
        f"{len(unifi_known_devices)}"
    )

    print(
        f"APs in Baseline: "
        f"{len(baseline_aps)}"
    )

    print(
        f"APs in Historie: "
        f"{len(ap_history)}"
    )

    print(
        f"Clients in Historie: "
        f"{len(client_history)}"
    )

    print(
        f"OUI-Einträge: "
        f"{len(oui_database)}"
    )

    print(
        f"Kanäle: {CHANNELS}"
    )

    print(
        f"Dwell: {DWELL_SECONDS}s"
    )

    print("Scoring:")
    print("  0-2  background")
    print("  3-4  observe")
    print("  5-6  interesting")
    print("  >=7  candidate")

    print(
        f"AP-Baseline: "
        f"{BASELINE_AP_CSV}"
    )

    print(
        f"AP-Historie: "
        f"{AP_HISTORY_CSV}"
    )

    print(
        f"Events: "
        f"{EVENT_JSONL}"
    )

    print(
        f"Channel-Activity: "
        f"{CHANNEL_ACTIVITY_JSONL}"
    )

    print(
        "Channel-Activity Fenster: "
        f"{CHANNEL_ACTIVITY_INTERVAL}s"
    )

    print(
        "Remote-ID Decoder: aktiv"
    )

    print(
        "Remote-ID Event-Dedupe: "
        f"{REMOTE_ID_EVENT_MIN_INTERVAL}s"
    )

    print(
        "Starte WLAN-Monitor ..."
    )

    hopper = threading.Thread(
        target=channel_hopper,
        daemon=True,
    )

    writer = threading.Thread(
        target=csv_writer,
        daemon=True,
    )

    unifi_reloader = threading.Thread(
        target=unifi_reload_worker,
        daemon=True,
    )

    activity_writer = threading.Thread(
        target=channel_activity_writer,
        daemon=True,
    )

    writer.start()
    hopper.start()
    unifi_reloader.start()
    activity_writer.start()

    try:
        sniff(
            iface=INTERFACE,
            prn=process_packet,
            store=False,
            stop_filter=lambda pkt:
                not running,
        )

    except KeyboardInterrupt:
        pass

    finally:
        with lock:
            history_rows = [
                dict(x)
                for x in ap_history.values()
            ]

        write_csv_file(
            AP_HISTORY_CSV,
            history_rows,
            [
                "bssid",
                "ssid",
                "manufacturer",
                "first_seen",
                "last_seen",
                "seen_count",
                "runs_seen",
                "last_channel",
                "last_frequency_mhz",
                "rssi_best_seen",
                "locally_administered",
                "baseline",
            ],
        )

        try:
            write_channel_activity_snapshot()
        except Exception as exc:
            print(
                f"[WARN] Finaler Channel-Activity Snapshot: {exc}"
            )

        print(
            "WLAN-Monitor beendet."
        )


if __name__ == "__main__":
    main()