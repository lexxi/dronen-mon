#!/usr/bin/env python3

"""
Passive Wi-Fi Remote-ID decoder for dronen-mon.

Design goals:
- no packet injection / no active radio behavior
- detect ASD-STAN / OpenDroneID-style Remote ID transported
  in IEEE 802.11 Vendor Specific Information Elements
- no external dependencies beyond Scapy, already used by wifi_monitor.py
- tolerant parsing: malformed payloads never crash the WLAN monitor

The supported Remote-ID OUIs mirror the current Cyber Defence Campus
RemoteIDReceiver implementation:
    FA:0B:BC
    50:6F:9A
    90:3A:E6

The RemoteIDReceiver currently skips an 8-byte Wi-Fi vendor payload header
before the Direct Remote ID message. This module follows that behavior but
also contains conservative fallbacks for field-layout differences seen in
Scapy captures.
"""

import struct
from datetime import datetime, timezone

try:
    from scapy.layers.dot11 import Dot11EltVendorSpecific
except Exception:
    Dot11EltVendorSpecific = None


VERSION = "1.2"

SUPPORTED_OUIS = {
    "FA:0B:BC",
    "50:6F:9A",
    "90:3A:E6",
}

MESSAGE_NAMES = {
    0x0: "basic_id",
    0x1: "location",
    0x2: "reserved",
    0x3: "self_id",
    0x4: "system",
    0x5: "operator_id",
    0xF: "message_pack",
}


def _oui_int_to_text(value):
    try:
        value = int(value)
    except Exception:
        return None

    if value < 0 or value > 0xFFFFFF:
        return None

    raw = f"{value:06X}"
    return f"{raw[0:2]}:{raw[2:4]}:{raw[4:6]}"


def _clean_ascii(data):
    if data is None:
        return None

    try:
        return bytes(data).decode("ascii", errors="replace").rstrip("\x00").strip()
    except Exception:
        return None


def _safe_unpack(fmt, payload):
    size = struct.calcsize(fmt)

    if len(payload) < size:
        raise ValueError(
            f"payload too short: need={size} have={len(payload)}"
        )

    return struct.unpack(fmt, payload[:size])


def _valid_printable_identifier(value, min_length=3, max_length=32):
    if value is None:
        return False

    value = value.strip()

    if not (min_length <= len(value) <= max_length):
        return False

    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def _decode_basic_id(payload):
    if len(payload) != 24:
        raise ValueError(
            f"Basic ID payload must be exactly 24 bytes, got {len(payload)}"
        )

    id_type = payload[0] >> 4
    ua_type = payload[0] & 0x0F
    uas_id = _clean_ascii(payload[1:])

    if not _valid_printable_identifier(uas_id):
        raise ValueError("Basic ID contains no plausible printable UAS ID")

    if id_type == 0x0F or ua_type == 0x0F:
        raise ValueError("Basic ID contains reserved identifier/type value")

    return {
        "type": "basic_id",
        "id_type": id_type,
        "ua_type": ua_type,
        "uas_id": uas_id,
    }


def _decode_location(payload):
    # Mirrors RemoteIDReceiver:
    # BBBbiiHHHBBHBB
    (
        status_byte,
        track_direction,
        speed_raw,
        vertical_speed_raw,
        latitude_raw,
        longitude_raw,
        barometric_raw,
        geodetic_raw,
        height_raw,
        hv_acc,
        baro_speed_acc,
        timestamp_raw,
        reserved,
        reserved2,
    ) = _safe_unpack("<BBBbiiHHHBBHBB", payload)

    status = status_byte >> 4
    height_type = (status_byte >> 2) & 0x01
    direction_sentiment = (status_byte >> 1) & 0x01
    speed_multiplier = status_byte & 0x01

    heading = track_direction + (180 if direction_sentiment else 0)

    if speed_multiplier:
        speed = (speed_raw * 0.75) + (255 * 0.25)
    else:
        speed = speed_raw * 0.25

    vertical_speed = vertical_speed_raw * 0.5

    latitude = latitude_raw / 10**7
    longitude = longitude_raw / 10**7

    if not (-90.0 <= latitude <= 90.0):
        raise ValueError(f"invalid latitude: {latitude}")

    if not (-180.0 <= longitude <= 180.0):
        raise ValueError(f"invalid longitude: {longitude}")

    barometric_altitude = (barometric_raw * 0.5) - 1000
    geodetic_altitude = (geodetic_raw * 0.5) - 1000
    height = (height_raw * 0.5) - 1000

    v_acc = (hv_acc >> 4) & 0x0F
    h_acc = hv_acc & 0x0F
    baro_acc = (baro_speed_acc >> 4) & 0x0F
    speed_acc = baro_speed_acc & 0x0F
    timestamp_acc = reserved & 0x0F

    return {
        "type": "location",
        "status": status,
        "height_type": height_type,
        "heading_deg": heading,
        "speed_mps": round(speed, 3),
        "vertical_speed_mps": round(vertical_speed, 3),
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "barometric_altitude_m": round(barometric_altitude, 1),
        "geodetic_altitude_m": round(geodetic_altitude, 1),
        "height_m": round(height, 1),
        "horizontal_accuracy": h_acc,
        "vertical_accuracy": v_acc,
        "speed_accuracy": speed_acc,
        "barometric_accuracy": baro_acc,
        "timestamp_accuracy": timestamp_acc,
        "protocol_timestamp_raw": timestamp_raw,
    }


def _decode_self_id(payload):
    if len(payload) != 24:
        raise ValueError(
            f"Self ID payload must be exactly 24 bytes, got {len(payload)}"
        )

    description_type = payload[0]
    description = _clean_ascii(payload[1:])

    if description and not _valid_printable_identifier(
        description,
        min_length=1,
        max_length=32,
    ):
        raise ValueError("Self ID contains non-printable description")

    return {
        "type": "self_id",
        "description_type": description_type,
        "description": description,
    }


def _decode_system(payload):
    # Mirrors RemoteIDReceiver:
    # <BiiHBHHBHxxxxx
    (
        flags,
        pilot_lat_raw,
        pilot_lon_raw,
        area_count,
        area_radius,
        area_ceiling_raw,
        area_floor_raw,
        ua_cat_class,
        pilot_geo_alt_raw,
    ) = _safe_unpack("<BiiHBHHBHxxxxx", payload)

    classification_type = (flags & 0b00011100) >> 2
    location_source = flags & 0b00000011

    return {
        "type": "system",
        "classification_type": classification_type,
        "operator_location_type": location_source,
        "operator_latitude": round(pilot_lat_raw / 10**7, 7),
        "operator_longitude": round(pilot_lon_raw / 10**7, 7),
        "area_count": area_count,
        "area_radius": area_radius,
        "area_ceiling_m": round((area_ceiling_raw * 0.5) - 1000, 1),
        "area_floor_m": round((area_floor_raw * 0.5) - 1000, 1),
        "ua_category_class": ua_cat_class,
        "operator_geodetic_altitude_m":
            round((pilot_geo_alt_raw * 0.5) - 1000, 1),
    }


def _decode_operator_id(payload):
    if len(payload) != 24:
        raise ValueError(
            f"Operator ID payload must be exactly 24 bytes, got {len(payload)}"
        )

    operator_id = _clean_ascii(payload[1:])

    if not _valid_printable_identifier(operator_id):
        raise ValueError(
            "Operator ID contains no plausible printable identifier"
        )

    return {
        "type": "operator_id",
        "operator_id_type": payload[0],
        "operator_id": operator_id,
    }


def _decode_single_message(message):
    if not message:
        raise ValueError("empty Remote ID message")

    msg_type = message[0] >> 4
    payload = message[1:]

    if msg_type == 0x0:
        return _decode_basic_id(payload)

    if msg_type == 0x1:
        return _decode_location(payload)

    if msg_type == 0x3:
        return _decode_self_id(payload)

    if msg_type == 0x4:
        return _decode_system(payload)

    if msg_type == 0x5:
        return _decode_operator_id(payload)

    if msg_type == 0x2:
        return {
            "type": "reserved",
            "raw_hex": payload.hex(),
        }

    if msg_type == 0xF:
        return _decode_message_pack(payload)

    return {
        "type": f"unknown_{msg_type:x}",
        "message_type": msg_type,
        "raw_hex": payload.hex(),
    }


def _decode_message_pack(payload):
    # Message-pack payload:
    # byte 0 message_size, byte 1 number_messages, then messages
    if len(payload) < 2:
        raise ValueError("Message pack payload too short")

    message_size = payload[0]
    message_count = payload[1]

    if message_size <= 0:
        raise ValueError("invalid Remote ID message size")

    messages = []

    for index in range(message_count):
        start = 2 + index * message_size
        end = start + message_size

        if end > len(payload):
            break

        raw_message = payload[start:end]

        try:
            decoded = _decode_single_message(raw_message)
        except Exception as exc:
            decoded = {
                "type": "decode_error",
                "error": str(exc),
                "raw_hex": raw_message.hex(),
            }

        messages.append(decoded)

    return {
        "type": "message_pack",
        "message_size": message_size,
        "message_count": message_count,
        "messages": messages,
    }


def _plausible_direct_message(data):
    """
    Strict structural validation before a Vendor Specific IE is accepted
    as Remote ID.

    Standard OpenDroneID/ASD-STAN direct messages are exactly 25 bytes
    including the message header. Message packs are variable length but
    must exactly match the advertised message size/count.
    """
    if not data:
        return False

    msg_type = data[0] >> 4

    if msg_type not in MESSAGE_NAMES:
        return False

    if msg_type in (0x0, 0x1, 0x3, 0x4, 0x5):
        return len(data) == 25

    if msg_type == 0x2:
        return False

    if msg_type == 0xF:
        if len(data) < 3:
            return False

        message_size = data[1]
        message_count = data[2]

        if message_size != 25:
            return False

        if message_count < 1 or message_count > 16:
            return False

        required = 3 + (message_size * message_count)
        return len(data) == required

    return False


def _candidate_messages(info):
    """
    RemoteIDReceiver currently calls parser.from_wifi(vendor_spec.info, oui)
    and the ASD-STAN parser starts at packet[8:].

    Depending on Scapy/version/capture source, the bytes presented via
    Dot11EltVendorSpecific.info can differ slightly. Try the canonical
    8-byte skip first, then conservative fallbacks.
    """
    raw = bytes(info or b"")

    candidates = []

    for offset in (8, 5, 4, 0):
        if len(raw) <= offset:
            continue

        candidate = raw[offset:]

        if _plausible_direct_message(candidate):
            candidates.append((offset, candidate))

    # Prefer protocol-shaped standard messages.
    candidates.sort(
        key=lambda item: (
            0 if item[0] == 8 else 1,
            0 if len(item[1]) >= 25 else 1,
            item[0],
        )
    )

    return candidates


def _flatten(decoded):
    """
    Produce convenient aggregate fields while retaining decoded_messages.
    """
    messages = []

    if decoded.get("type") == "message_pack":
        messages.extend(decoded.get("messages", []))
    else:
        messages.append(decoded)

    result = {
        "message_types": [],
        "decoded_messages": messages,
    }

    for message in messages:
        msg_type = message.get("type")

        if msg_type and msg_type not in result["message_types"]:
            result["message_types"].append(msg_type)

        if msg_type == "basic_id":
            if message.get("uas_id"):
                result["uas_id"] = message["uas_id"]

            result["id_type"] = message.get("id_type")
            result["ua_type"] = message.get("ua_type")

        elif msg_type == "location":
            for key in (
                "latitude",
                "longitude",
                "geodetic_altitude_m",
                "barometric_altitude_m",
                "height_m",
                "speed_mps",
                "vertical_speed_mps",
                "heading_deg",
                "status",
            ):
                if key in message:
                    result[key] = message[key]

        elif msg_type == "operator_id":
            if message.get("operator_id"):
                result["operator_id"] = message["operator_id"]

        elif msg_type == "system":
            for key in (
                "operator_latitude",
                "operator_longitude",
                "operator_geodetic_altitude_m",
                "operator_location_type",
                "classification_type",
            ):
                if key in message:
                    result[key] = message[key]

        elif msg_type == "self_id":
            if message.get("description"):
                result["self_description"] = message["description"]

    return result


def decode_remote_id_packet(packet):
    """
    Inspect all Vendor Specific IEs in a Scapy Wi-Fi packet.

    Returns:
        list[dict]: zero or more decoded Remote-ID detections.
    """
    if Dot11EltVendorSpecific is None:
        return []

    try:
        layer = packet.getlayer(Dot11EltVendorSpecific)
    except Exception:
        return []

    detections = []

    while layer:
        try:
            oui = _oui_int_to_text(layer.oui)
        except Exception:
            oui = None

        if oui in SUPPORTED_OUIS:
            raw_info = bytes(getattr(layer, "info", b"") or b"")

            detection = {
                "protocol": "ASD-STAN Direct Remote ID",
                "oui": oui,
                "remote_id_detected": True,
                "vendor_info_length": len(raw_info),
            }

            candidates = _candidate_messages(raw_info)

            if candidates:
                offset, direct_message = candidates[0]

                detection["payload_offset"] = offset
                detection["message_type"] = (
                    direct_message[0] >> 4
                )
                detection["message_type_name"] = MESSAGE_NAMES.get(
                    detection["message_type"],
                    f"unknown_{detection['message_type']:x}",
                )

                try:
                    decoded = _decode_single_message(direct_message)
                    detection["decode_ok"] = True
                    detection.update(_flatten(decoded))

                except Exception as exc:
                    detection["decode_ok"] = False
                    detection["decode_error"] = str(exc)
                    detection["raw_remote_id_hex"] = direct_message.hex()

            else:
                # A supported OUI alone is not sufficient evidence of
                # Remote ID (notably 50:6F:9A is also used by Wi-Fi Alliance
                # vendor IEs). Invalid/too-short payloads are ignored.
                try:
                    layer = layer.payload.getlayer(Dot11EltVendorSpecific)
                except Exception:
                    layer = None
                continue

            # Only successfully decoded, structurally valid Remote-ID
            # messages become detections.
            if detection.get("decode_ok"):
                detections.append(detection)

        try:
            layer = layer.payload.getlayer(Dot11EltVendorSpecific)
        except Exception:
            break

    return detections


def build_remote_id_event(
    detection,
    *,
    timestamp,
    bssid,
    transmitter,
    rssi,
    channel,
    frequency_mhz,
):
    event = {
        "event": "remote_id_detected",
        "timestamp": timestamp,
        "bssid": bssid,
        "transmitter": transmitter,
        "rssi": rssi,
        "channel": channel,
        "frequency_mhz": frequency_mhz,
        "confidence": "protocol_indicator",
        **detection,
    }

    return event


if __name__ == "__main__":
    print(
        f"remote_id_decoder.py V{VERSION} "
        f"OUIs={','.join(sorted(SUPPORTED_OUIS))}"
    )

