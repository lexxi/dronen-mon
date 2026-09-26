#!/usr/bin/env python3

import csv
import fcntl
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CONFIG_FILE = Path("/opt/dronen-mon/config/unifi.json")
OUTPUT_FILE = Path("/opt/dronen-mon/config/unifi_clients.csv")
HISTORY_FILE = Path(
    "/opt/dronen-mon/data/unifi/wireless_clients.jsonl"
)
HISTORY_LOCK_FILE = Path(
    "/opt/dronen-mon/data/.history.lock"
)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_mac(value):
    if not value:
        return ""
    return value.strip().lower()


def load_config():
    if not CONFIG_FILE.exists():
        raise RuntimeError(f"Konfiguration fehlt: {CONFIG_FILE}")

    with CONFIG_FILE.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    for key in ("base_url", "site_id", "api_key_file"):
        if not cfg.get(key):
            raise RuntimeError(f"Pflichtfeld fehlt in {CONFIG_FILE}: {key}")

    return cfg


def read_api_key(path):
    path = Path(path)

    if not path.exists():
        raise RuntimeError(f"API-Key-Datei fehlt: {path}")

    key = path.read_text(encoding="utf-8").strip()

    if not key:
        raise RuntimeError(f"API-Key-Datei ist leer: {path}")

    return key


def fetch_json_url(cfg, api_key, url):
    request = urllib.request.Request(
        url,
        headers={
            "X-API-KEY": api_key,
            "Accept": "application/json",
            "User-Agent": "dronen-mon-unifi-import/1.1",
        },
        method="GET",
    )

    verify_tls = bool(cfg.get("verify_tls", False))

    if verify_tls:
        context = ssl.create_default_context()
    else:
        context = ssl._create_unverified_context()

    timeout = int(cfg.get("timeout_seconds", 15))

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=context,
        ) as response:
            payload = response.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            f"UniFi HTTP {exc.code}: {detail[:500]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"UniFi nicht erreichbar: {exc}"
        ) from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "UniFi lieferte kein gültiges JSON"
        ) from exc


def fetch_clients(cfg, api_key):
    base_url = cfg["base_url"].rstrip("/")
    site_id = cfg["site_id"]

    url = (
        f"{base_url}/proxy/network/integration/v1/"
        f"sites/{site_id}/clients?limit=200"
    )

    return fetch_json_url(
        cfg,
        api_key,
        url,
    )


def fetch_station_stats(cfg, api_key):
    base_url = cfg["base_url"].rstrip("/")
    site_name = cfg.get(
        "site_name",
        "default",
    )

    url = (
        f"{base_url}/proxy/network/api/s/"
        f"{site_name}/stat/sta"
    )

    return fetch_json_url(
        cfg,
        api_key,
        url,
    )


def fetch_devices(cfg, api_key):
    base_url = cfg["base_url"].rstrip("/")
    site_name = cfg.get(
        "site_name",
        "default",
    )

    url = (
        f"{base_url}/proxy/network/api/s/"
        f"{site_name}/stat/device"
    )

    return fetch_json_url(
        cfg,
        api_key,
        url,
    )


def first_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key)

        if value not in (None, ""):
            return value

    return None


def station_stats_by_mac(payload):
    result = {}

    for station in payload.get("data", []):
        mac = normalize_mac(
            station.get("mac")
        )

        if mac:
            result[mac] = station

    return result


def extract_ap_radios(payload):
    radios = []

    for device in payload.get("data", []):
        device_type = str(
            device.get("type", "")
        ).lower()

        if device_type not in (
            "uap",
            "ux",
            "udm",
        ):
            continue

        ap_name = (
            device.get("name")
            or device.get("hostname")
            or device.get("model")
            or ""
        )
        ap_mac = normalize_mac(
            device.get("mac")
        )

        # Fuer den tatsaechlich aktiven Kanal radio_table_stats
        # bevorzugen. radio_table enthaelt bei Auto-Kanalwahl oft
        # nur "channel": "auto".
        radio_rows = (
            device.get("radio_table_stats")
            or device.get("radio_table")
            or []
        )

        for radio in radio_rows:
            channel = first_value(
                radio,
                "channel",
                "channel_num",
            )

            try:
                channel = int(channel)
            except (
                TypeError,
                ValueError,
            ):
                channel = None

            if channel is None:
                continue

            radios.append({
                "ap_name": ap_name,
                "ap_mac": ap_mac,
                "radio": first_value(
                    radio,
                    "radio",
                    "radio_name",
                ),
                "radio_name": first_value(
                    radio,
                    "name",
                    "radio_name",
                ),
                "channel": channel,
                "channel_width": first_value(
                    radio,
                    "bw",
                    "ht",
                    "channel_width",
                    "width",
                ),
                "tx_power_dbm": first_value(
                    radio,
                    "tx_power",
                    "tx_power_dbm",
                ),
                "tx_power_mode": first_value(
                    radio,
                    "tx_power_mode",
                ),
            })

    radios.sort(
        key=lambda row: (
            row["ap_name"],
            row["channel"],
            str(row.get("radio") or ""),
        )
    )

    return radios


def extract_wireless_clients(
    payload,
    station_stats=None,
):
    rows = []
    imported_at = now_iso()
    station_stats = station_stats or {}

    for client in payload.get("data", []):
        if str(client.get("type", "")).upper() != "WIRELESS":
            continue

        mac = normalize_mac(client.get("macAddress"))

        if not mac:
            continue

        station = station_stats.get(
            mac,
            {},
        )

        channel = first_value(
            station,
            "channel",
            "wifiChannel",
            "radioChannel",
            "uplinkChannel",
        )
        signal_dbm = first_value(
            station,
            "signal",
            "signalStrength",
        )
        rssi = first_value(
            station,
            "rssi",
        )
        noise_dbm = first_value(
            station,
            "noise",
        )
        ssid = first_value(
            station,
            "essid",
            "ssid",
            "wifiSsid",
            "networkName",
        )
        radio = first_value(
            station,
            "radio",
            "radio_name",
            "last_radio",
        )
        radio_name = first_value(
            station,
            "radio_name",
        )
        ap_name = first_value(
            station,
            "last_uplink_name",
            "uplinkDeviceName",
            "apName",
            "accessPointName",
        )
        ap_mac = normalize_mac(
            first_value(
                station,
                "ap_mac",
                "last_uplink_mac",
            )
        )
        bssid = normalize_mac(
            first_value(
                station,
                "bssid",
            )
        )
        tx_rate = first_value(
            station,
            "tx_rate",
        )
        rx_rate = first_value(
            station,
            "rx_rate",
        )

        rows.append({
            "mac": mac,
            "name": (client.get("name") or "").strip(),
            "ip": (client.get("ipAddress") or "").strip(),
            "connected_at": (client.get("connectedAt") or "").strip(),
            "client_id": (client.get("id") or "").strip(),
            "uplink_device_id": (client.get("uplinkDeviceId") or "").strip(),
            "channel": channel,
            "signal_dbm": signal_dbm,
            "rssi": rssi,
            "noise_dbm": noise_dbm,
            "ssid": ssid,
            "radio": radio,
            "radio_name": radio_name,
            "ap_name": ap_name,
            "ap_mac": ap_mac,
            "bssid": bssid,
            "tx_rate": tx_rate,
            "rx_rate": rx_rate,
            "source": "unifi",
            "imported_at": imported_at,
        })

    rows.sort(key=lambda row: row["mac"])
    return rows


def write_csv(rows):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp = Path(str(OUTPUT_FILE) + ".tmp")

    fields = [
        "mac",
        "name",
        "ip",
        "connected_at",
        "client_id",
        "uplink_device_id",
        "channel",
        "signal_dbm",
        "rssi",
        "noise_dbm",
        "ssid",
        "radio",
        "radio_name",
        "ap_name",
        "ap_mac",
        "bssid",
        "tx_rate",
        "rx_rate",
        "source",
        "imported_at",
    ]

    with temp.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
        )
        writer.writeheader()
        writer.writerows(rows)

    os.replace(temp, OUTPUT_FILE)


def append_history(rows, ap_radios=None):
    HISTORY_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ap_radios = ap_radios or []

    snapshot = {
        "timestamp": (
            rows[0]["imported_at"]
            if rows
            else now_iso()
        ),
        "client_count": len(rows),
        "clients": rows,
        "ap_radio_count": len(ap_radios),
        "ap_radios": ap_radios,
    }

    with HISTORY_FILE.open(
        "a",
        encoding="utf-8",
    ) as fh:
        fh.write(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        fh.write("\n")


def main():
    cfg = load_config()
    api_key = read_api_key(cfg["api_key_file"])

    payload = fetch_clients(
        cfg,
        api_key,
    )
    station_payload = fetch_station_stats(
        cfg,
        api_key,
    )
    device_payload = fetch_devices(
        cfg,
        api_key,
    )
    stations = station_stats_by_mac(
        station_payload
    )
    ap_radios = extract_ap_radios(
        device_payload
    )
    rows = extract_wireless_clients(
        payload,
        stations,
    )
    write_csv(rows)
    HISTORY_LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with HISTORY_LOCK_FILE.open("a+") as lock_fh:
        fcntl.flock(
            lock_fh.fileno(),
            fcntl.LOCK_EX,
        )
        append_history(
            rows,
            ap_radios,
        )

    print(
        f"UniFi-Import OK: "
        f"{len(rows)} Wireless-Clients, "
        f"{len(ap_radios)} AP-Radios -> {OUTPUT_FILE}; "
        f"Historie -> {HISTORY_FILE}"
    )

    for row in rows:
        print(
            f"  {row['mac']}  "
            f"{row['ip']:<15}  "
            f"CH={str(row['channel']):<3}  "
            f"signal={str(row['signal_dbm']):<4}  "
            f"{row['name']}"
        )

    if ap_radios:
        print("AP-Radios:")
        for radio in ap_radios:
            print(
                f"  {radio['ap_name']:<20} "
                f"CH={radio['channel']:<3} "
                f"radio={radio.get('radio')} "
                f"width={radio.get('channel_width')} "
                f"tx={radio.get('tx_power_dbm')}"
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise SystemExit(1)
