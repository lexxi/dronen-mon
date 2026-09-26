#!/usr/bin/env python3

import csv
import re
from pathlib import Path


RESERVATIONS = Path("/etc/dnsmasq.d/dhcp-reservations.conf")
LEASES = Path("/var/lib/misc/dnsmasq.leases")
OUTPUT = Path("/opt/dronen-mon/known_devices.csv")


MAC_RE = re.compile(
    r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$"
)


def norm_mac(value):
    value = value.strip().lower()
    return value if MAC_RE.match(value) else None


devices = {}


# Reservierungen
if RESERVATIONS.exists():
    with RESERVATIONS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if not line.startswith("dhcp-host="):
                continue

            parts = line[len("dhcp-host="):].split(",")

            if not parts:
                continue

            mac = norm_mac(parts[0])

            if not mac:
                continue

            ip = ""
            label = ""

            for part in parts[1:]:
                part = part.strip()

                if re.match(r"^\d+\.\d+\.\d+\.\d+$", part):
                    ip = part
                elif part and not part.lower().startswith("set:"):
                    label = part

            devices[mac] = {
                "mac": mac,
                "label": label,
                "ip": ip,
                "source": "reservation",
            }


# Leases
if LEASES.exists():
    with LEASES.open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()

            if len(parts) < 4:
                continue

            mac = norm_mac(parts[1])

            if not mac:
                continue

            ip = parts[2]
            hostname = parts[3]

            # Reservierung gewinnt
            if mac in devices:
                continue

            devices[mac] = {
                "mac": mac,
                "label": "" if hostname == "*" else hostname,
                "ip": ip,
                "source": "lease",
            }


rows = sorted(
    devices.values(),
    key=lambda x: (
        x["label"].lower(),
        x["mac"]
    )
)

with OUTPUT.open(
    "w",
    newline="",
    encoding="utf-8"
) as fh:

    writer = csv.DictWriter(
        fh,
        fieldnames=[
            "mac",
            "label",
            "ip",
            "source",
        ],
    )

    writer.writeheader()
    writer.writerows(rows)


print(f"{len(rows)} bekannte Geräte geschrieben.")
print(f"Datei: {OUTPUT}")
