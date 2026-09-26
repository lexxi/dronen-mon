#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


VERSION = "1.4"

BASE_DIR = Path("/opt/dronen-mon")
BIN_DIR = BASE_DIR / "bin"

DATA_DIR = BASE_DIR / "data" / "sdr"
EVENT_DIR = DATA_DIR / "events"
PERSISTENT_DIR = DATA_DIR / "persistent"

EVENT_FILE = EVENT_DIR / "sdr_events.jsonl"

ANALYZER = BIN_DIR / "sdr_analyze.py"

DEFAULT_EVENT_DELTA = 6.0
DEFAULT_MATCH_TOLERANCE_HZ = 150_000
DEFAULT_DEDUP_TOLERANCE_HZ = 150_000

DEFAULT_PERSISTENT_MIN_SWEEPS = 3
DEFAULT_PERSISTENT_MIN_AGE_SECONDS = 300
DEFAULT_PERSISTENT_TOLERANCE_HZ = 150_000


def now_iso():
    return (
        datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def now_epoch():
    return time.time()


def parse_iso(value):
    if not value:
        return None

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.timestamp()

    except Exception:
        return None


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Kontinuierlicher SDR-Baseline-Monitor "
            "mit persistenter RF-Lernschicht"
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
        "--baseline",
        required=True,
    )

    parser.add_argument(
        "--device",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--usb-path",
        default=None,
    )

    parser.add_argument(
        "--duration",
        default="2m",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--sleep",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--event-delta",
        type=float,
        default=DEFAULT_EVENT_DELTA,
    )

    parser.add_argument(
        "--match-tolerance",
        type=int,
        default=DEFAULT_MATCH_TOLERANCE_HZ,
    )

    parser.add_argument(
        "--dedup-tolerance",
        type=int,
        default=DEFAULT_DEDUP_TOLERANCE_HZ,
    )

    parser.add_argument(
        "--persistent-min-sweeps",
        type=int,
        default=DEFAULT_PERSISTENT_MIN_SWEEPS,
    )

    parser.add_argument(
        "--persistent-min-age",
        type=int,
        default=DEFAULT_PERSISTENT_MIN_AGE_SECONDS,
    )

    parser.add_argument(
        "--persistent-tolerance",
        type=int,
        default=DEFAULT_PERSISTENT_TOLERANCE_HZ,
    )

    return parser.parse_args()


def persistent_filename(sensor_name):
    safe = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        sensor_name,
    )

    return (
        PERSISTENT_DIR
        / f"persistent_{safe}.json"
    )


def read_usb_bus_device(usb_path):
    device_dir = (
        Path("/sys/bus/usb/devices")
        / usb_path
    )

    if not device_dir.exists():
        raise RuntimeError(
            f"USB-Pfad nicht vorhanden: {usb_path}"
        )

    bus_file = device_dir / "busnum"
    dev_file = device_dir / "devnum"

    if (
        not bus_file.exists()
        or not dev_file.exists()
    ):
        raise RuntimeError(
            f"busnum/devnum fehlen für {usb_path}"
        )

    return (
        int(bus_file.read_text().strip()),
        int(dev_file.read_text().strip()),
    )


def list_rtl_devices():
    result = subprocess.run(
        ["lsusb"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
        )

    devices = []

    pattern = re.compile(
        r"^Bus\s+(\d+)\s+"
        r"Device\s+(\d+):\s+"
        r"ID\s+0bda:2832\b"
    )

    for line in result.stdout.splitlines():
        match = pattern.search(line)

        if not match:
            continue

        devices.append(
            (
                int(match.group(1)),
                int(match.group(2)),
            )
        )

    return devices


def resolve_device_index_from_usb_path(
    usb_path,
):
    devices = list_rtl_devices()

    if not devices:
        raise RuntimeError(
            "Keine RTL2832U-Geräte gefunden."
        )

    # Normalfall: exakter USB-Pfad existiert.
    try:
        wanted = read_usb_bus_device(
            usb_path
        )

        if wanted in devices:
            return devices.index(wanted)

    except RuntimeError:
        pass

    # Fallback:
    # Wenn sich nach Reboot die USB-Portnummer geändert hat,
    # auf demselben USB-Bus nach RTL2832U suchen.
    try:
        wanted_bus = int(
            usb_path.split("-", 1)[0]
        )

    except (ValueError, IndexError):
        raise RuntimeError(
            f"Ungültiger USB-Pfad: {usb_path}"
        )

    candidates = [
        (index, bus, dev)
        for index, (bus, dev)
        in enumerate(devices)
        if bus == wanted_bus
    ]

    if len(candidates) == 1:
        index, bus, dev = candidates[0]

        print(
            f"[SDR USB FALLBACK] "
            f"Pfad {usb_path} nicht gefunden; "
            f"verwende RTL2832U auf "
            f"Bus {bus:03d} Device {dev:03d} "
            f"(rtl index {index})",
            flush=True,
        )

        return index

    readable = ", ".join(
        f"{bus:03d}:{dev:03d}"
        for bus, dev in devices
    )

    if not candidates:
        raise RuntimeError(
            f"USB-Pfad {usb_path} nicht gefunden "
            f"und kein RTL2832U auf Bus "
            f"{wanted_bus:03d}. "
            f"Gefunden: {readable}"
        )

    raise RuntimeError(
        f"USB-Pfad {usb_path} nicht gefunden "
        f"und mehrere RTL2832U auf Bus "
        f"{wanted_bus:03d}; "
        f"keine eindeutige Zuordnung möglich. "
        f"Gefunden: {readable}"
    )


def resolve_device(args):
    if args.usb_path:
        return resolve_device_index_from_usb_path(
            args.usb_path
        )

    if args.device is not None:
        return args.device

    return 0


def load_baseline(filename):
    clusters = []

    with open(
        filename,
        newline="",
        encoding="utf-8",
    ) as fh:

        reader = csv.DictReader(fh)

        for row in reader:
            try:
                clusters.append({
                    "center_hz":
                        float(row["center_mhz"])
                        * 1_000_000,

                    "start_hz":
                        float(row["start_mhz"])
                        * 1_000_000,

                    "end_hz":
                        float(row["end_mhz"])
                        * 1_000_000,
                })

            except (
                ValueError,
                KeyError,
            ):
                continue

    return clusters


def is_known_frequency(
    freq_hz,
    baseline,
    tolerance_hz,
):
    for cluster in baseline:

        if (
            cluster["start_hz"] - tolerance_hz
            <= freq_hz
            <= cluster["end_hz"] + tolerance_hz
        ):
            return True

    return False


def load_persistent_history(filename):
    if not filename.exists():
        return []

    try:
        with filename.open(
            encoding="utf-8",
        ) as fh:

            data = json.load(fh)

        if not isinstance(data, list):
            return []

        return data

    except Exception:
        return []


def save_persistent_history(
    filename,
    entries,
):
    filename.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = Path(
        str(filename) + ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8",
    ) as fh:

        json.dump(
            entries,
            fh,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp,
        filename,
    )


def find_persistent_cluster(
    entries,
    freq_hz,
    tolerance_hz,
):
    for entry in entries:

        center_hz = float(
            entry.get(
                "center_frequency_mhz",
                0,
            )
        ) * 1_000_000

        if (
            abs(
                center_hz
                - freq_hz
            )
            <= tolerance_hz
        ):
            return entry

    return None


def is_learned_persistent(
    entry,
):
    return bool(
        entry.get(
            "learned",
            False,
        )
    )


def run_capture(
    args,
    output,
    device_index,
):
    command = [
        "rtl_power",
        "-d",
        str(device_index),
        "-f",
        args.frequency_range,
        "-i",
        str(args.interval),
        "-e",
        args.duration,
        output,
    ]

    return subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_analysis(
    raw_file,
    peak_file,
):
    return subprocess.run(
        [
            str(ANALYZER),
            raw_file,

            "--min-peak-delta",
            "5",

            "--min-median-delta",
            "2",

            "--output",
            peak_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def load_current_peaks(filename):
    peaks = []

    if not os.path.exists(
        filename
    ):
        return peaks

    with open(
        filename,
        newline="",
        encoding="utf-8",
    ) as fh:

        reader = csv.DictReader(fh)

        for row in reader:
            try:
                center_mhz = float(
                    row["center_mhz"]
                )

                peaks.append({
                    "frequency_mhz":
                        center_mhz,

                    "frequency_hz":
                        center_mhz
                        * 1_000_000,

                    "start_mhz":
                        float(
                            row["start_mhz"]
                        ),

                    "end_mhz":
                        float(
                            row["end_mhz"]
                        ),

                    "median_delta_db":
                        float(
                            row[
                                "median_delta_db"
                            ]
                        ),

                    "peak_delta_db":
                        float(
                            row[
                                "peak_delta_db"
                            ]
                        ),

                    "classification":
                        row.get(
                            "classification",
                            "",
                        ),
                })

            except (
                ValueError,
                KeyError,
            ):
                continue

    return peaks


def filter_baseline_peaks(
    peaks,
    baseline,
    event_delta,
    match_tolerance,
):
    result = []

    for peak in peaks:

        if (
            peak["peak_delta_db"]
            < event_delta
        ):
            continue

        if is_known_frequency(
            peak["frequency_hz"],
            baseline,
            match_tolerance,
        ):
            continue

        result.append(
            peak
        )

    return result


def deduplicate_peaks(
    peaks,
    tolerance_hz,
):
    if not peaks:
        return []

    ordered = sorted(
        peaks,
        key=lambda x:
            x["frequency_hz"],
    )

    groups = []
    current = [ordered[0]]

    for peak in ordered[1:]:

        previous = current[-1]

        if (
            peak["frequency_hz"]
            - previous["frequency_hz"]
            <= tolerance_hz
        ):
            current.append(
                peak
            )

        else:
            groups.append(
                current
            )

            current = [
                peak
            ]

    groups.append(
        current
    )

    deduped = []

    for group in groups:

        strongest = max(
            group,
            key=lambda x:
                x["peak_delta_db"],
        ).copy()

        strongest[
            "merged_peaks"
        ] = len(group)

        strongest[
            "cluster_start_mhz"
        ] = min(
            x["start_mhz"]
            for x in group
        )

        strongest[
            "cluster_end_mhz"
        ] = max(
            x["end_mhz"]
            for x in group
        )

        deduped.append(
            strongest
        )

    return deduped


def update_persistent_history(
    args,
    entries,
    signals,
):
    """
    Neue Cluster werden zunächst beobachtet.

    Erst wenn:
      - Analyzer persistent
      - mindestens N Sweeps
      - mindestens X Sekunden sichtbar

    wird learned=True.
    """

    changed = False
    timestamp = now_iso()
    epoch = now_epoch()

    for signal in signals:

        freq_hz = (
            signal["frequency_hz"]
        )

        entry = (
            find_persistent_cluster(
                entries,
                freq_hz,
                args.persistent_tolerance,
            )
        )

        if entry is None:

            entry = {
                "center_frequency_mhz":
                    signal[
                        "frequency_mhz"
                    ],

                "first_seen":
                    timestamp,

                "last_seen":
                    timestamp,

                "seen_sweeps":
                    1,

                "max_peak_delta_db":
                    signal[
                        "peak_delta_db"
                    ],

                "last_peak_delta_db":
                    signal[
                        "peak_delta_db"
                    ],

                "classification":
                    signal[
                        "classification"
                    ],

                "learned":
                    False,
            }

            entries.append(
                entry
            )

            changed = True

        else:

            entry["last_seen"] = (
                timestamp
            )

            entry["seen_sweeps"] = (
                int(
                    entry.get(
                        "seen_sweeps",
                        0,
                    )
                )
                + 1
            )

            entry[
                "last_peak_delta_db"
            ] = signal[
                "peak_delta_db"
            ]

            entry[
                "max_peak_delta_db"
            ] = max(
                float(
                    entry.get(
                        "max_peak_delta_db",
                        0,
                    )
                ),
                signal[
                    "peak_delta_db"
                ],
            )

            # Mittelpunkt langsam
            # Richtung aktueller Messung
            # verschieben.
            old_center = float(
                entry[
                    "center_frequency_mhz"
                ]
            )

            entry[
                "center_frequency_mhz"
            ] = round(
                (
                    old_center
                    * 0.8
                    + signal[
                        "frequency_mhz"
                    ]
                    * 0.2
                ),
                6,
            )

            changed = True

        first_epoch = parse_iso(
            entry.get(
                "first_seen"
            )
        )

        age = 0

        if first_epoch:
            age = (
                epoch
                - first_epoch
            )

        if (
            not entry.get(
                "learned"
            )
            and signal.get(
                "classification"
            )
            == "persistent"
            and int(
                entry.get(
                    "seen_sweeps",
                    0,
                )
            )
            >= args.persistent_min_sweeps
            and age
            >= args.persistent_min_age
        ):
            entry[
                "learned"
            ] = True

            entry[
                "learned_at"
            ] = timestamp

            changed = True

            print(
                "[SDR LEARN] "
                f"sensor={args.name} "
                f"freq="
                f"{entry['center_frequency_mhz']:.3f}MHz "
                f"sweeps="
                f"{entry['seen_sweeps']} "
                f"age={age:.0f}s "
                f"maxΔ="
                f"{entry['max_peak_delta_db']:.2f}dB",
                flush=True,
            )

    return changed


def remove_learned_signals(
    args,
    entries,
    signals,
):
    result = []

    for signal in signals:

        entry = (
            find_persistent_cluster(
                entries,
                signal[
                    "frequency_hz"
                ],
                args.persistent_tolerance,
            )
        )

        if (
            entry
            and is_learned_persistent(
                entry
            )
        ):
            continue

        result.append(
            signal
        )

    return result


def write_sweep_event(
    args,
    device_index,
    signals,
):
    if not signals:
        return

    strongest = max(
        signals,
        key=lambda x:
            x["peak_delta_db"],
    )

    sweep_id = str(
        uuid.uuid4()
    )

    event = {
        "timestamp":
            now_iso(),

        "event_type":
            "rf_sweep_activity",

        "sweep_id":
            sweep_id,

        "sensor":
            args.name,

        "usb_path":
            args.usb_path,

        "device_index":
            device_index,

        "frequency_range":
            args.frequency_range,

        "signal_count":
            len(signals),

        "strongest_frequency_mhz":
            strongest[
                "frequency_mhz"
            ],

        "max_peak_delta_db":
            strongest[
                "peak_delta_db"
            ],

        "signals": [
            {
                "frequency_mhz":
                    signal[
                        "frequency_mhz"
                    ],

                "start_mhz":
                    signal[
                        "cluster_start_mhz"
                    ],

                "end_mhz":
                    signal[
                        "cluster_end_mhz"
                    ],

                "peak_delta_db":
                    signal[
                        "peak_delta_db"
                    ],

                "median_delta_db":
                    signal[
                        "median_delta_db"
                    ],

                "classification":
                    signal[
                        "classification"
                    ],

                "merged_peaks":
                    signal[
                        "merged_peaks"
                    ],
            }
            for signal in signals
        ],
    }

    with EVENT_FILE.open(
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

    print(
        "[SDR SWEEP] "
        f"sensor={args.name} "
        f"signals={len(signals)} "
        f"strongest="
        f"{strongest['frequency_mhz']:.3f}MHz "
        f"Δ="
        f"{strongest['peak_delta_db']:.2f}dB",
        flush=True,
    )


def main():
    args = parse_args()

    EVENT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    PERSISTENT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    EVENT_FILE.touch(
        exist_ok=True,
    )

    persistent_file = (
        persistent_filename(
            args.name
        )
    )

    persistent_history = (
        load_persistent_history(
            persistent_file
        )
    )

    if not persistent_file.exists():
        save_persistent_history(
            persistent_file,
            persistent_history,
        )

    baseline_file = Path(
        args.baseline
    )

    if not baseline_file.is_absolute():
        baseline_file = (
            DATA_DIR
            / "baselines"
            / baseline_file
        )

    if not baseline_file.exists():
        raise SystemExit(
            f"Baseline nicht gefunden: "
            f"{baseline_file}"
        )

    baseline = load_baseline(
        baseline_file
    )

    try:
        device_index = (
            resolve_device(
                args
            )
        )

    except Exception as exc:
        raise SystemExit(
            "SDR-Gerät konnte "
            f"nicht aufgelöst werden: {exc}"
        )

    learned_count = sum(
        1
        for x in persistent_history
        if x.get("learned")
    )

    print()
    print(
        f"SDR-Monitor V{VERSION}"
    )

    print(
        f"Sensor: {args.name}"
    )

    print(
        f"Bereich: "
        f"{args.frequency_range}"
    )

    if args.usb_path:
        print(
            f"USB-Pfad: "
            f"{args.usb_path}"
        )

    print(
        f"Aktueller rtl-sdr Index: "
        f"{device_index}"
    )

    print(
        f"Baseline: "
        f"{baseline_file}"
    )

    print(
        f"Baseline-Cluster: "
        f"{len(baseline)}"
    )

    print(
        f"Event-Schwelle: "
        f"{args.event_delta:.1f} dB"
    )

    print(
        f"Sweep-Dedup: "
        f"±{args.dedup_tolerance} Hz"
    )

    print(
        f"Persistent-Historie: "
        f"{persistent_file}"
    )

    print(
        f"Persistent-Einträge: "
        f"{len(persistent_history)}"
    )

    print(
        f"Davon gelernt: "
        f"{learned_count}"
    )

    print(
        "Persistent-Regel: "
        f">={args.persistent_min_sweeps} Sweeps, "
        f">={args.persistent_min_age}s"
    )

    print(
        f"Event-Datei: "
        f"{EVENT_FILE}"
    )

    print(
        "Starte SDR-Monitor ..."
    )

    try:
        while True:

            if args.usb_path:

                try:
                    device_index = (
                        resolve_device_index_from_usb_path(
                            args.usb_path
                        )
                    )

                except Exception as exc:

                    print(
                        "[WARN] "
                        f"{args.name}: "
                        f"{exc}",
                        flush=True,
                    )

                    time.sleep(
                        args.sleep
                    )

                    continue

            with tempfile.TemporaryDirectory(
                prefix=f"sdr_{args.name}_"
            ) as tmpdir:

                raw_file = os.path.join(
                    tmpdir,
                    "capture.csv",
                )

                peak_file = os.path.join(
                    tmpdir,
                    "peaks.csv",
                )

                capture = run_capture(
                    args,
                    raw_file,
                    device_index,
                )

                if capture.returncode != 0:

                    print(
                        "[WARN] rtl_power "
                        f"sensor={args.name}: "
                        f"{capture.stderr.strip()}",
                        flush=True,
                    )

                    time.sleep(
                        args.sleep
                    )

                    continue

                analysis = run_analysis(
                    raw_file,
                    peak_file,
                )

                if analysis.returncode != 0:

                    print(
                        "[WARN] Analyse "
                        f"sensor={args.name}: "
                        f"{analysis.stderr.strip()}",
                        flush=True,
                    )

                    time.sleep(
                        args.sleep
                    )

                    continue

                signals = (
                    load_current_peaks(
                        peak_file
                    )
                )

                signals = (
                    filter_baseline_peaks(
                        signals,
                        baseline,
                        args.event_delta,
                        args.match_tolerance,
                    )
                )

                signals = (
                    deduplicate_peaks(
                        signals,
                        args.dedup_tolerance,
                    )
                )

                changed = (
                    update_persistent_history(
                        args,
                        persistent_history,
                        signals,
                    )
                )

                if changed:
                    save_persistent_history(
                        persistent_file,
                        persistent_history,
                    )

                signals_for_event = (
                    remove_learned_signals(
                        args,
                        persistent_history,
                        signals,
                    )
                )

                write_sweep_event(
                    args,
                    device_index,
                    signals_for_event,
                )

            time.sleep(
                args.sleep
            )

    except KeyboardInterrupt:
        print()

        print(
            f"SDR-Monitor "
            f"{args.name} beendet."
        )


if __name__ == "__main__":
    main()
