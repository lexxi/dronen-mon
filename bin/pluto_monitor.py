#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


VERSION = "0.1"

BASE_DIR = Path("/opt/dronen-mon")
DATA_DIR = BASE_DIR / "data" / "pluto"
EVENT_DIR = DATA_DIR / "events"
EVENT_FILE = EVENT_DIR / "pluto_rf.jsonl"

DEFAULT_SAMPLE_RATE = 20_000_000
DEFAULT_CAPTURE_SECONDS = 0.15
DEFAULT_SLEEP = 0.20

# Bewusst zunächst grob verteilt.
SCAN_FREQUENCIES = [
    # 2.4 GHz
    ("2.4G", 2_407_000_000),
    ("2.4G", 2_427_000_000),
    ("2.4G", 2_447_000_000),
    ("2.4G", 2_467_000_000),
    ("2.4G", 2_477_000_000),

    # 5.8 GHz
    ("5.8G", 5_735_000_000),
    ("5.8G", 5_755_000_000),
    ("5.8G", 5_775_000_000),
    ("5.8G", 5_795_000_000),
    ("5.8G", 5_815_000_000),
    ("5.8G", 5_835_000_000),
]


def now_iso():
    return (
        datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def run_command(command, timeout=10):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def find_pluto_uri():
    result = run_command(
        ["iio_info", "-s"],
        timeout=10,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or "iio_info -s fehlgeschlagen"
        )

    for line in result.stdout.splitlines():
        if "PlutoSDR" not in line:
            continue

        match = re.search(
            r"\[(usb:[^\]]+)\]",
            line,
        )

        if match:
            return match.group(1)

    raise RuntimeError(
        "Kein PlutoSDR USB-Kontext gefunden."
    )


def set_rx_frequency(uri, frequency_hz):
    result = run_command(
        [
            "iio_attr",
            "-u",
            uri,
            "-c",
            "ad9361-phy",
            "RX_LO",
            "frequency",
            str(frequency_hz),
        ]
    )

    if result.returncode != 0:
        raise RuntimeError(
            "RX_LO konnte nicht gesetzt werden: "
            + result.stderr.strip()
        )


def set_sample_rate(uri, sample_rate):
    # PHY RX sampling frequency
    result = run_command(
        [
            "iio_attr",
            "-u",
            uri,
            "-c",
            "ad9361-phy",
            "voltage0",
            "sampling_frequency",
            str(sample_rate),
        ]
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Samplingrate konnte nicht gesetzt werden: "
            + result.stderr.strip()
        )


def capture_iq(
    uri,
    seconds,
    buffer_size=65536,
):
    with tempfile.NamedTemporaryFile(
        prefix="pluto_",
        suffix=".iq",
        delete=False,
    ) as fh:
        filename = fh.name

    try:
        command = [
            "timeout",
            str(seconds),
            "iio_readdev",
            "-u",
            uri,
            "-b",
            str(buffer_size),
            "cf-ad9361-lpc",
            "voltage0",
            "voltage1",
        ]

        with open(filename, "wb") as output:
            result = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.PIPE,
            )

        # timeout liefert normalerweise 124.
        if result.returncode not in (0, 124):
            raise RuntimeError(
                result.stderr.decode(
                    errors="replace"
                ).strip()
            )

        data = np.fromfile(
            filename,
            dtype="<i2",
        )

        if len(data) < 4:
            raise RuntimeError(
                "Zu wenig IQ-Daten empfangen."
            )

        # Interleaved I,Q
        if len(data) % 2:
            data = data[:-1]

        i_data = data[0::2].astype(
            np.float32
        )
        q_data = data[1::2].astype(
            np.float32
        )

        return i_data + 1j * q_data

    finally:
        try:
            os.unlink(filename)
        except FileNotFoundError:
            pass


def analyze_iq(
    samples,
    sample_rate,
    center_frequency,
):
    # Begrenzen, damit FFT nicht unnötig riesig wird.
    fft_size = min(
        262144,
        len(samples),
    )

    if fft_size < 4096:
        raise RuntimeError(
            "Nicht genügend Samples für FFT."
        )

    samples = samples[:fft_size]

    # DC Offset reduzieren
    samples = samples - np.mean(samples)

    window = np.hanning(
        fft_size
    ).astype(np.float32)

    spectrum = np.fft.fftshift(
        np.fft.fft(
            samples * window
        )
    )

    power = (
        np.abs(spectrum) ** 2
    )

    power_db = 10.0 * np.log10(
        power + 1e-12
    )

    frequencies = (
        np.fft.fftshift(
            np.fft.fftfreq(
                fft_size,
                d=1.0 / sample_rate,
            )
        )
        + center_frequency
    )

    # Pluto-DC-Spike direkt um LO ausblenden.
    dc_mask = (
        np.abs(
            frequencies
            - center_frequency
        )
        > 200_000
    )

    valid_power = power_db[
        dc_mask
    ]

    valid_freq = frequencies[
        dc_mask
    ]

    if not len(valid_power):
        raise RuntimeError(
            "Keine gültigen FFT-Bins."
        )

    peak_index = int(
        np.argmax(valid_power)
    )

    peak_db = float(
        valid_power[peak_index]
    )

    peak_frequency = float(
        valid_freq[peak_index]
    )

    median_db = float(
        np.median(valid_power)
    )

    mean_db = float(
        np.mean(valid_power)
    )

    return {
        "peak_frequency_hz":
            int(peak_frequency),

        "peak_frequency_mhz":
            round(
                peak_frequency
                / 1_000_000,
                6,
            ),

        "peak_db":
            round(peak_db, 2),

        "median_db":
            round(median_db, 2),

        "mean_db":
            round(mean_db, 2),

        "peak_over_median_db":
            round(
                peak_db - median_db,
                2,
            ),
    }


def append_event(event):
    EVENT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Passiver ADALM-Pluto "
            "2.4/5.8 GHz RF Monitor"
        )
    )

    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
    )

    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=DEFAULT_CAPTURE_SECONDS,
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
    )

    return parser.parse_args()


def main():
    args = parse_args()

    print(
        f"Pluto RF Monitor V{VERSION}",
        flush=True,
    )

    uri = find_pluto_uri()

    print(
        f"Pluto URI: {uri}",
        flush=True,
    )

    print(
        f"Samplingrate: "
        f"{args.sample_rate}",
        flush=True,
    )

    print(
        f"Capture: "
        f"{args.capture_seconds}s",
        flush=True,
    )

    print(
        f"Event-Datei: {EVENT_FILE}",
        flush=True,
    )

    set_sample_rate(
        uri,
        args.sample_rate,
    )

    while True:
        for band, frequency in SCAN_FREQUENCIES:
            try:
                # URI kann sich nach USB-Reconnect ändern.
                # Falls Aufnahme scheitert, wird sie
                # im nächsten Zyklus neu gesucht.
                if not uri:
                    uri = find_pluto_uri()

                set_rx_frequency(
                    uri,
                    frequency,
                )

                # LO settling
                time.sleep(0.05)

                samples = capture_iq(
                    uri,
                    args.capture_seconds,
                )

                analysis = analyze_iq(
                    samples,
                    args.sample_rate,
                    frequency,
                )

                event = {
                    "timestamp":
                        now_iso(),

                    "sensor":
                        "pluto",

                    "band":
                        band,

                    "center_frequency_hz":
                        frequency,

                    "center_frequency_mhz":
                        round(
                            frequency
                            / 1_000_000,
                            3,
                        ),

                    "sample_rate":
                        args.sample_rate,

                    "sample_count":
                        int(len(samples)),

                    **analysis,
                }

                append_event(event)

                print(
                    "[PLUTO] "
                    f"band={band} "
                    f"center="
                    f"{frequency / 1e6:.3f}MHz "
                    f"peak="
                    f"{analysis['peak_frequency_mhz']:.3f}MHz "
                    f"delta="
                    f"{analysis['peak_over_median_db']:.2f}dB",
                    flush=True,
                )

            except Exception as exc:
                print(
                    "[PLUTO ERROR] "
                    f"band={band} "
                    f"center="
                    f"{frequency / 1e6:.3f}MHz "
                    f"{exc}",
                    flush=True,
                )

                # Bei USB-/URI-Fehler neu suchen.
                uri = None

                time.sleep(1)

            time.sleep(
                args.sleep
            )


if __name__ == "__main__":
    main()
