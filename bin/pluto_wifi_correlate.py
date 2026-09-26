#!/usr/bin/env python3

import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path


VERSION = "2.6"

PLUTO_FILE = Path(
    "/opt/dronen-mon/data/pluto/events/pluto_rf.jsonl"
)

WIFI_FILE = Path(
    "/opt/dronen-mon/data/wifi/channel_activity.jsonl"
)

UNIFI_HISTORY_FILE = Path(
    "/opt/dronen-mon/data/unifi/wireless_clients.jsonl"
)

CANDIDATE_FILE = Path(
    "/opt/dronen-mon/data/pluto/candidates.jsonl"
)

CANDIDATE_CURRENT_FILE = Path(
    "/opt/dronen-mon/data/pluto/candidates_current.json"
)

TIME_WINDOW_SECONDS = 25
UNIFI_TIME_WINDOW_SECONDS = 360
WIFI_CHANNEL_HALF_WIDTH_MHZ = 10.0

ANALYSIS_WINDOW_SECONDS = 72 * 60 * 60

BASELINE_REFERENCE_SECONDS = 7200
BASELINE_RECENT_SECONDS = 7200
BASELINE_MIN_SAMPLES_PER_CENTER = 50

WIFI_CHANNELS = {
    1: 2412.0,
    6: 2437.0,
    11: 2462.0,
    149: 5745.0,
    153: 5765.0,
    157: 5785.0,
    161: 5805.0,
    165: 5825.0,
}

WIFI_CHANNELS_24_ALL = {
    **{
        channel: 2407.0 + 5.0 * channel
        for channel in range(1, 14)
    },
    14: 2484.0,
}

# Wiederkehrende, sehr stabile Peaks aus der lokalen Baseline.
# Diese werden vorerst als fester RF-Hintergrund behandelt.
KNOWN_FIXED_PEAKS_MHZ = [
    2399.415,
    2426.005,
    2450.000,
    2479.991,
]

FIXED_PEAK_TOLERANCE_MHZ = 0.15

RECURRENT_PEAK_TOLERANCE_MHZ = 0.20
RECURRENT_PEAK_MIN_COUNT = 3
RECURRENT_PEAK_MIN_SPAN_SECONDS = 60

RECURRENT_REGION_WIDTH_MHZ = 10.0
RECURRENT_REGION_MIN_COUNT = 4
RECURRENT_REGION_MIN_SPAN_SECONDS = 600
RECURRENT_REGION_MAX_MAD_DB = 0.75
RECURRENT_REGION_MEMBER_MIN_TOLERANCE_DB = 1.0


def parse_time(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).timestamp()


def median(values):
    values = sorted(values)

    if not values:
        return None

    n = len(values)
    mid = n // 2

    if n % 2:
        return values[mid]

    return (
        values[mid - 1]
        + values[mid]
    ) / 2.0


def median_absolute_deviation(values):
    center = median(values)

    if center is None:
        return None

    deviations = [
        abs(value - center)
        for value in values
    ]

    return median(deviations)


def percentile(values, p):
    values = sorted(values)

    if not values:
        return None

    pos = (len(values) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)

    if lo == hi:
        return values[lo]

    frac = pos - lo

    return (
        values[lo]
        + (values[hi] - values[lo])
        * frac
    )


def load_pluto(min_epoch=None):
    rows = []

    with PLUTO_FILE.open(
        encoding="utf-8"
    ) as fh:
        for line in fh:
            try:
                row = json.loads(line)
                row["_epoch"] = parse_time(
                    row["timestamp"]
                )

                if (
                    min_epoch is not None
                    and row["_epoch"] < min_epoch
                ):
                    continue

                row["peak_over_median_db"] = float(
                    row["peak_over_median_db"]
                )
                row["center_frequency_mhz"] = float(
                    row["center_frequency_mhz"]
                )
                row["peak_frequency_mhz"] = float(
                    row["peak_frequency_mhz"]
                )
                rows.append(row)

            except Exception:
                continue

    return rows


def load_wifi():
    rows_all = []
    visited_epochs = []

    with WIFI_FILE.open(
        encoding="utf-8"
    ) as fh:
        for line in fh:
            try:
                row = json.loads(line)

                row["_epoch"] = parse_time(
                    row["timestamp"]
                )

                row["channel"] = int(
                    row["channel"]
                )

                rows_all.append(row)

                if row.get(
                    "channel_visited"
                ) is True:
                    visited_epochs.append(
                        row["_epoch"]
                    )

            except Exception:
                continue

    # V2.4 kennzeichnet jeden tatsächlich besuchten Kanal,
    # auch wenn dort 0 Frames empfangen wurden. Wenn solche
    # Datensätze vorhanden sind, beginnt die belastbare
    # Vergleichsperiode mit dem ersten V2.4-Snapshot.
    if visited_epochs:
        coverage_start = min(
            visited_epochs
        )
        rows_all = [
            row
            for row in rows_all
            if row["_epoch"] >= coverage_start
        ]
    elif rows_all:
        coverage_start = min(
            row["_epoch"]
            for row in rows_all
        )
    else:
        return defaultdict(list), None, None

    by_channel = defaultdict(list)

    for row in rows_all:
        by_channel[
            row["channel"]
        ].append(row)

    for rows in by_channel.values():
        rows.sort(
            key=lambda x: x["_epoch"]
        )

    coverage_end = max(
        row["_epoch"]
        for row in rows_all
    )

    return (
        by_channel,
        coverage_start,
        coverage_end,
    )


def load_unifi_history():
    snapshots = []

    if not UNIFI_HISTORY_FILE.exists():
        return snapshots

    with UNIFI_HISTORY_FILE.open(
        encoding="utf-8"
    ) as fh:
        for line in fh:
            try:
                row = json.loads(line)
                row["_epoch"] = parse_time(
                    row["timestamp"]
                )

                for client in row.get(
                    "clients",
                    [],
                ):
                    value = client.get(
                        "channel"
                    )

                    try:
                        client["channel"] = (
                            int(value)
                            if value not in (
                                None,
                                "",
                            )
                            else None
                        )
                    except (
                        TypeError,
                        ValueError,
                    ):
                        client["channel"] = None

                snapshots.append(row)

            except Exception:
                continue

    snapshots.sort(
        key=lambda row: row["_epoch"]
    )

    return snapshots


def nearest_unifi_snapshot(
    snapshots,
    epoch,
):
    if not snapshots:
        return None

    times = [
        row["_epoch"]
        for row in snapshots
    ]

    pos = bisect_left(
        times,
        epoch,
    )

    candidates = []

    if pos < len(snapshots):
        candidates.append(
            snapshots[pos]
        )

    if pos > 0:
        candidates.append(
            snapshots[pos - 1]
        )

    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda row: abs(
            row["_epoch"] - epoch
        ),
    )

    if (
        abs(best["_epoch"] - epoch)
        > UNIFI_TIME_WINDOW_SECONDS
    ):
        return None

    return best


def unifi_context(
    snapshots,
    epoch,
    channel,
):
    snapshot = nearest_unifi_snapshot(
        snapshots,
        epoch,
    )

    if snapshot is None:
        return {
            "unifi_snapshot": None,
            "unifi_clients_total": None,
            "unifi_clients_channel": None,
            "unifi_clients_unknown_channel": None,
            "unifi_ap_radios_total": None,
            "unifi_ap_radios_channel": None,
        }

    clients = snapshot.get(
        "clients",
        [],
    )

    known_channel = [
        client
        for client in clients
        if client.get("channel")
        is not None
    ]

    same_channel = [
        client
        for client in known_channel
        if (
            channel is not None
            and client.get("channel")
            == channel
        )
    ]

    ap_radios = snapshot.get(
        "ap_radios"
    )
    ap_radio_count = snapshot.get(
        "ap_radio_count"
    )

    ap_radio_data_valid = (
        isinstance(ap_radios, list)
        and ap_radio_count is not None
        and int(ap_radio_count) > 0
    )

    same_ap_channel = (
        [
            radio
            for radio in ap_radios
            if (
                channel is not None
                and radio.get("channel")
                == channel
            )
        ]
        if ap_radio_data_valid
        else []
    )

    return {
        "unifi_snapshot":
            snapshot.get("timestamp"),
        "unifi_clients_total":
            len(clients),
        "unifi_clients_channel":
            (
                len(same_channel)
                if channel is not None
                else None
            ),
        "unifi_clients_unknown_channel":
            len(clients)
            - len(known_channel),
        "unifi_ap_radios_total":
            (
                len(ap_radios)
                if ap_radio_data_valid
                else None
            ),
        "unifi_ap_radios_channel":
            (
                len(same_ap_channel)
                if (
                    ap_radio_data_valid
                    and channel is not None
                )
                else None
            ),
    }


def unifi_ap_coverage(snapshots):
    valid = [
        row
        for row in snapshots
        if (
            isinstance(
                row.get("ap_radios"),
                list,
            )
            and row.get(
                "ap_radio_count"
            ) is not None
            and int(
                row.get(
                    "ap_radio_count",
                    0,
                )
            ) > 0
        )
    ]

    if not valid:
        return None, None, 0

    return (
        valid[0]["_epoch"],
        valid[-1]["_epoch"],
        len(valid),
    )


def channel_coverage(wifi_by_channel):
    coverage = {}

    for channel, rows in wifi_by_channel.items():
        visited = [
            row
            for row in rows
            if row.get(
                "channel_visited"
            ) is True
        ]

        source = visited or rows

        if not source:
            continue

        coverage[channel] = (
            source[0]["_epoch"],
            source[-1]["_epoch"],
        )

    return coverage


def build_thresholds(
    pluto_rows,
    reference_start,
    coverage_end,
):
    reference_end = (
        reference_start
        + BASELINE_REFERENCE_SECONDS
    )
    recent_start = max(
        reference_start,
        coverage_end
        - BASELINE_RECENT_SECONDS,
    )

    fixed = defaultdict(list)
    recent = defaultdict(list)
    fallback = defaultdict(list)

    for row in pluto_rows:
        key = (
            row["band"],
            row["center_frequency_mhz"],
        )

        value = row[
            "peak_over_median_db"
        ]

        fallback[key].append(value)

        if (
            reference_start
            <= row["_epoch"]
            <= reference_end
        ):
            fixed[key].append(value)

        if (
            recent_start
            <= row["_epoch"]
            <= coverage_end
        ):
            recent[key].append(value)

    thresholds = {}
    sources = {}

    for key, all_values in fallback.items():
        fixed_values = fixed.get(
            key,
            [],
        )
        recent_values = recent.get(
            key,
            [],
        )

        fixed_p99 = (
            percentile(fixed_values, 0.99)
            if len(fixed_values)
            >= BASELINE_MIN_SAMPLES_PER_CENTER
            else None
        )
        recent_p99 = (
            percentile(recent_values, 0.99)
            if len(recent_values)
            >= BASELINE_MIN_SAMPLES_PER_CENTER
            else None
        )

        window_p99 = percentile(
            all_values,
            0.99,
        )

        if (
            fixed_p99 is not None
            and recent_p99 is not None
        ):
            thresholds[key] = max(
                fixed_p99,
                recent_p99,
                window_p99,
            )
            sources[key] = "hybrid_window_max"
        elif fixed_p99 is not None:
            thresholds[key] = max(
                fixed_p99,
                window_p99,
            )
            sources[key] = "fixed_window_max"
        elif recent_p99 is not None:
            thresholds[key] = max(
                recent_p99,
                window_p99,
            )
            sources[key] = "recent_window_max"
        else:
            thresholds[key] = window_p99
            sources[key] = "window_p99"

    return (
        thresholds,
        sources,
        reference_end,
        recent_start,
    )

def nearest_wifi_channel(
    frequency_mhz
):
    best_channel = None
    best_distance = None

    for channel, center in (
        WIFI_CHANNELS.items()
    ):
        distance = abs(
            frequency_mhz - center
        )

        if (
            best_distance is None
            or distance < best_distance
        ):
            best_channel = channel
            best_distance = distance

    # Ein 20-MHz-Kanal wird hier nur innerhalb
    # seiner ungefähren +/-10-MHz-Bandbreite
    # dem beobachteten WLAN-Kanal zugerechnet.
    if (
        best_distance
        > WIFI_CHANNEL_HALF_WIDTH_MHZ
    ):
        return None

    return best_channel


def nearest_standard_24_channel(
    frequency_mhz
):
    best_channel = None
    best_distance = None

    for channel, center in (
        WIFI_CHANNELS_24_ALL.items()
    ):
        distance = abs(
            frequency_mhz - center
        )

        if (
            best_distance is None
            or distance < best_distance
        ):
            best_channel = channel
            best_distance = distance

    return best_channel, best_distance


def nearest_activity(
    rows,
    epoch,
):
    if not rows:
        return None

    times = [
        row["_epoch"]
        for row in rows
    ]

    pos = bisect_left(
        times,
        epoch,
    )

    candidates = []

    if pos < len(rows):
        candidates.append(
            rows[pos]
        )

    if pos > 0:
        candidates.append(
            rows[pos - 1]
        )

    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda x:
            abs(
                x["_epoch"]
                - epoch
            ),
    )

    distance = abs(
        best["_epoch"] - epoch
    )

    if distance > TIME_WINDOW_SECONDS:
        return None

    return best


def is_fixed_background_peak(
    frequency_mhz
):
    for known in KNOWN_FIXED_PEAKS_MHZ:
        if (
            abs(frequency_mhz - known)
            <= FIXED_PEAK_TOLERANCE_MHZ
        ):
            return known

    return None


def classify(
    pluto,
    wifi_by_channel,
    coverage_by_channel,
):
    peak_freq = pluto[
        "peak_frequency_mhz"
    ]

    fixed_peak = (
        is_fixed_background_peak(
            peak_freq
        )
    )

    if fixed_peak is not None:
        return {
            "classification":
                "fixed_rf_background",
            "reason":
                (
                    "Peak entspricht wiederkehrendem "
                    "lokalem RF-Hintergrund"
                ),
            "fixed_peak_mhz":
                fixed_peak,
        }

    if 2402.0 <= peak_freq <= 2494.0:
        (
            standard_channel,
            standard_distance,
        ) = nearest_standard_24_channel(
            peak_freq
        )

        if (
            standard_distance is not None
            and standard_distance
            <= WIFI_CHANNEL_HALF_WIDTH_MHZ
            and standard_channel
            not in (1, 6, 11)
        ):
            return {
                "classification":
                    "wifi_channel_not_observed",
                "wifi_channel":
                    standard_channel,
                "reason":
                    (
                        "Peak liegt im 2.4-GHz-WLAN-Band "
                        "auf einem nicht überwachten Kanal"
                    ),
            }

    channel = nearest_wifi_channel(
        peak_freq
    )

    if channel is None:
        if 2402.0 <= peak_freq <= 2494.0:
            return {
                "classification":
                    "wifi_channel_not_observed",
                "reason":
                    (
                        "Peak liegt im 2.4-GHz-WLAN-Band, "
                        "aber außerhalb der überwachten "
                        "Kanäle 1/6/11"
                    ),
            }

        return {
            "classification":
                "non_wifi_rf_candidate",
            "reason":
                (
                    "Peak liegt außerhalb der "
                    "überwachten WLAN-Kanalbereiche"
                ),
        }

    coverage = coverage_by_channel.get(
        channel
    )

    if (
        coverage is None
        or pluto["_epoch"] < coverage[0]
        or pluto["_epoch"] > coverage[1]
    ):
        return {
            "classification":
                "wifi_activity_missing",
            "wifi_channel":
                channel,
            "reason":
                (
                    "Pluto-Treffer liegt außerhalb "
                    "der Messperiode dieses WLAN-Kanals"
                ),
        }

    activity = nearest_activity(
        wifi_by_channel.get(
            channel,
            [],
        ),
        pluto["_epoch"],
    )

    if activity is None:
        return {
            "classification":
                "wifi_activity_missing",
            "wifi_channel":
                channel,
            "reason":
                (
                    "Für den zugeordneten Kanal liegt "
                    "im Zeitfenster keine WLAN-Messung vor"
                ),
        }

    frames = int(
        activity.get(
            "frames_total",
            0,
        )
    )

    data_frames = int(
        activity.get(
            "data_frames",
            0,
        )
    )

    management = int(
        activity.get(
            "management_frames",
            0,
        )
    )

    result = {
        "wifi_channel": channel,
        "wifi_frames": frames,
        "wifi_data_frames":
            data_frames,
        "wifi_management_frames":
            management,
        "wifi_strongest_rssi":
            activity.get(
                "strongest_rssi"
            ),
    }

    if frames >= 50:
        result[
            "classification"
        ] = "wifi_explained"

        result["reason"] = (
            "Starke gleichzeitige "
            "802.11-Aktivität"
        )

    elif frames >= 10:
        result[
            "classification"
        ] = "wifi_uncertain"

        result["reason"] = (
            "Etwas gleichzeitige "
            "802.11-Aktivität"
        )

    else:
        result[
            "classification"
        ] = "non_wifi_rf_candidate"

        result["reason"] = (
            "Starker RF-Peak bei "
            "geringer WLAN-Aktivität"
        )

    return result


def mark_recurrent_background(results):
    candidates = [
        row
        for row in results
        if row["classification"]
        in (
            "non_wifi_rf_candidate",
            "wifi_activity_missing",
        )
    ]

    candidates.sort(
        key=lambda row: row["peak_mhz"]
    )

    # Kandidaten sind bereits nach Frequenz sortiert.
    # Daher reicht ein laufender Cluster; ein späterer
    # Wert, der nicht mehr in den aktuellen Cluster passt,
    # kann auch in keinen früheren Cluster mehr passen.
    clusters = []
    current = []
    current_sum = 0.0

    for row in candidates:
        if not current:
            current = [row]
            current_sum = row["peak_mhz"]
            continue

        center = current_sum / len(current)

        if (
            abs(row["peak_mhz"] - center)
            <= RECURRENT_PEAK_TOLERANCE_MHZ
        ):
            current.append(row)
            current_sum += row["peak_mhz"]
        else:
            clusters.append(current)
            current = [row]
            current_sum = row["peak_mhz"]

    if current:
        clusters.append(current)

    for cluster in clusters:
        if len(cluster) < RECURRENT_PEAK_MIN_COUNT:
            continue

        epochs = sorted(
            row["_epoch"]
            for row in cluster
        )

        span = epochs[-1] - epochs[0]

        if span < RECURRENT_PEAK_MIN_SPAN_SECONDS:
            continue

        center = sum(
            row["peak_mhz"]
            for row in cluster
        ) / len(cluster)

        for row in cluster:
            row["classification"] = (
                "recurrent_rf_background"
            )
            row["recurrent_peak_mhz"] = round(
                center,
                6,
            )
            row["recurrent_count"] = len(
                cluster
            )
            row["reason"] = (
                "Wiederkehrender RF-Peak an nahezu "
                "identischer Frequenz"
            )


def mark_recurrent_regions(results):
    candidates = [
        row
        for row in results
        if row["classification"]
        == "non_wifi_rf_candidate"
    ]

    groups = defaultdict(list)

    for row in candidates:
        key = (
            row["band"],
            row["center_mhz"],
        )
        groups[key].append(row)

    for group_rows in groups.values():
        group_rows.sort(
            key=lambda row: row["peak_mhz"]
        )

        regions = []
        current_region = []
        region_min = None

        for row in group_rows:
            frequency = row["peak_mhz"]

            if not current_region:
                current_region = [row]
                region_min = frequency
                continue

            if (
                frequency - region_min
                <= RECURRENT_REGION_WIDTH_MHZ
            ):
                current_region.append(row)
            else:
                regions.append(current_region)
                current_region = [row]
                region_min = frequency

        if current_region:
            regions.append(current_region)

        for region in regions:
            if (
                len(region)
                < RECURRENT_REGION_MIN_COUNT
            ):
                continue

            epochs = sorted(
                row["_epoch"]
                for row in region
            )

            span = (
                epochs[-1] - epochs[0]
            )

            if (
                span
                < RECURRENT_REGION_MIN_SPAN_SECONDS
            ):
                continue

            deltas = [
                row["delta_db"]
                for row in region
            ]

            delta_median = median(
                deltas
            )
            delta_mad = (
                median_absolute_deviation(
                    deltas
                )
            )

            if (
                delta_mad is None
                or delta_mad
                > RECURRENT_REGION_MAX_MAD_DB
            ):
                continue

            member_tolerance = max(
                RECURRENT_REGION_MEMBER_MIN_TOLERANCE_DB,
                4.0 * delta_mad,
            )

            freqs = [
                row["peak_mhz"]
                for row in region
            ]

            region_min = min(freqs)
            region_max = max(freqs)

            for row in region:
                deviation = abs(
                    row["delta_db"]
                    - delta_median
                )

                if (
                    deviation
                    > member_tolerance
                ):
                    row[
                        "region_outlier"
                    ] = True
                    row[
                        "region_median_db"
                    ] = round(
                        delta_median,
                        2,
                    )
                    row[
                        "region_mad_db"
                    ] = round(
                        delta_mad,
                        2,
                    )
                    continue

                row["classification"] = (
                    "recurrent_rf_region"
                )
                row["region_min_mhz"] = round(
                    region_min,
                    6,
                )
                row["region_max_mhz"] = round(
                    region_max,
                    6,
                )
                row["region_count"] = len(
                    region
                )
                row["region_median_db"] = round(
                    delta_median,
                    2,
                )
                row["region_mad_db"] = round(
                    delta_mad,
                    2,
                )
                row["reason"] = (
                    "Wiederkehrende RF-Region mit "
                    "robuster Pegelcharakteristik"
                )


def score_candidate(row):
    classification = row.get(
        "classification"
    )

    # Harte Hintergrund-/Ausschlussklassen.
    if classification in (
        "fixed_rf_background",
        "recurrent_rf_background",
    ):
        return 0, "background"

    if classification in (
        "recurrent_rf_region",
        "wifi_channel_not_observed",
        "wifi_explained",
    ):
        return 1, "background-like"

    score = 0

    frames = row.get(
        "wifi_frames"
    )

    if frames is not None:
        if frames == 0:
            score += 2
        elif frames < 10:
            score += 1
        elif frames >= 50:
            score -= 2
        else:
            score -= 1

    ux7_channel = row.get(
        "unifi_clients_channel"
    )

    if ux7_channel is not None:
        if ux7_channel == 0:
            score += 2
        else:
            score -= 2

    ux7_ap_channel = row.get(
        "unifi_ap_radios_channel"
    )

    if ux7_ap_channel is not None:
        if ux7_ap_channel == 0:
            score += 1
        else:
            score -= 1

    if row.get("band") == "5.8G":
        score += 1

    threshold = row.get(
        "threshold_db"
    )
    delta = row.get(
        "delta_db"
    )

    if (
        threshold is not None
        and delta is not None
    ):
        excess = delta - threshold

        if excess >= 6.0:
            score += 2
        elif excess >= 3.0:
            score += 1

    if row.get(
        "region_outlier"
    ) is True:
        score += 1

    if classification == "wifi_uncertain":
        score -= 1

    score = max(
        0,
        min(9, score),
    )

    if score >= 7:
        label = "candidate"
    elif score >= 5:
        label = "interesting"
    elif score >= 3:
        label = "observe"
    else:
        label = "background-like"

    return score, label


def apply_candidate_scores(results):
    for row in results:
        (
            row["candidate_score"],
            row["candidate_level"],
        ) = score_candidate(row)


def candidate_event_id(row):
    return "|".join([
        str(row.get("timestamp", "")),
        str(row.get("band", "")),
        f"{float(row.get('center_mhz', 0.0)):.6f}",
        f"{float(row.get('peak_mhz', 0.0)):.6f}",
    ])


def load_candidate_history():
    rows = []

    if not CANDIDATE_FILE.exists():
        return rows

    with CANDIDATE_FILE.open(
        encoding="utf-8"
    ) as fh:
        for line in fh:
            try:
                row = json.loads(line)
                if row.get("event_id"):
                    rows.append(row)
            except Exception:
                continue

    return rows


def serializable_candidate(row):
    persisted = {
        key: value
        for key, value in row.items()
        if key != "_epoch"
    }
    persisted["event_id"] = candidate_event_id(row)
    return persisted


def persist_candidate_state(results):
    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    current = [
        row
        for row in results
        if row.get("candidate_score", 0) >= 3
    ]
    current.sort(
        key=lambda row: row["_epoch"],
        reverse=True,
    )

    CANDIDATE_CURRENT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    current_rows = []
    for row in current:
        item = serializable_candidate(row)
        item["evaluated_at"] = now
        item["correlator_version"] = VERSION
        current_rows.append(item)

    with CANDIDATE_CURRENT_FILE.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            current_rows,
            fh,
            ensure_ascii=False,
            indent=2,
        )
        fh.write("\n")

    result_by_id = {
        candidate_event_id(row): row
        for row in results
    }

    history = load_candidate_history()
    history_by_id = {
        row["event_id"]: row
        for row in history
        if row.get("event_id")
    }

    added = 0

    for row in current:
        event_id = candidate_event_id(row)
        if event_id in history_by_id:
            continue

        item = serializable_candidate(row)
        score = row.get("candidate_score")
        level = row.get("candidate_level")

        item.update({
            "persisted_at": now,
            "correlator_version": VERSION,
            "first_score": score,
            "max_score": score,
            "latest_score": score,
            "first_level": level,
            "latest_level": level,
            "last_evaluated": now,
            "currently_candidate": True,
        })

        history.append(item)
        history_by_id[event_id] = item
        added += 1

    for item in history:
        event_id = item.get("event_id")
        current_result = result_by_id.get(event_id)

        if "first_score" not in item:
            item["first_score"] = item.get(
                "candidate_score"
            )
        if "first_level" not in item:
            item["first_level"] = item.get(
                "candidate_level"
            )
        if "max_score" not in item:
            item["max_score"] = item.get(
                "candidate_score"
            )

        if current_result is None:
            item["latest_score"] = None
            item["latest_level"] = (
                "not_in_current_p99"
            )
            item["currently_candidate"] = False
        else:
            latest_score = current_result.get(
                "candidate_score"
            )
            latest_level = current_result.get(
                "candidate_level"
            )
            item["latest_score"] = latest_score
            item["latest_level"] = latest_level
            item["currently_candidate"] = (
                latest_score is not None
                and latest_score >= 3
            )

            previous_max = item.get(
                "max_score"
            )
            if (
                latest_score is not None
                and (
                    previous_max is None
                    or latest_score > previous_max
                )
            ):
                item["max_score"] = latest_score

        item["last_evaluated"] = now

    history.sort(
        key=lambda row: row.get(
            "timestamp",
            "",
        )
    )

    with CANDIDATE_FILE.open(
        "w",
        encoding="utf-8",
    ) as fh:
        for row in history:
            fh.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            fh.write("\n")

    return added, len(current_rows)



def main():
    (
        wifi,
        coverage_start,
        coverage_end,
    ) = load_wifi()

    if (
        coverage_start is None
        or coverage_end is None
    ):
        raise SystemExit(
            "Keine WLAN-Channel-Activity-Daten vorhanden."
        )

    # V2.6: Die aufwendige Korrelation arbeitet nur noch
    # auf einem rollenden 72-Stunden-Fenster. Rohdaten und
    # Candidate-Historie bleiben davon unberührt.
    analysis_start = max(
        coverage_start,
        coverage_end - ANALYSIS_WINDOW_SECONDS,
    )

    # Pluto wird zwar weiterhin sequenziell aus JSONL gelesen,
    # ältere Datensätze werden aber bereits beim Einlesen
    # verworfen und nicht mehr im Speicher gehalten.
    pluto_all = load_pluto(
        min_epoch=analysis_start
    )

    unifi_history = load_unifi_history()

    (
        unifi_ap_start,
        unifi_ap_end,
        unifi_ap_snapshots,
    ) = unifi_ap_coverage(
        unifi_history
    )

    coverage_start = analysis_start

    wifi = defaultdict(
        list,
        {
            channel: [
                row
                for row in rows
                if row["_epoch"] >= analysis_start
            ]
            for channel, rows in wifi.items()
        },
    )

    unifi_history = [
        row
        for row in unifi_history
        if row["_epoch"] >= (
            analysis_start
            - UNIFI_TIME_WINDOW_SECONDS
        )
    ]

    # Nur Daten auswerten, für die beide Sensoren
    # gleichzeitig im rollenden Fenster Messdaten besitzen.
    pluto_rows = [
        row
        for row in pluto_all
        if (
            analysis_start
            <= row["_epoch"]
            <= coverage_end
        )
    ]

    (
        thresholds,
        threshold_sources,
        baseline_reference_end,
        baseline_recent_start,
    ) = build_thresholds(
        pluto_rows,
        coverage_start,
        coverage_end,
    )

    coverage_by_channel = (
        channel_coverage(wifi)
    )

    results = []

    for row in pluto_rows:
        key = (
            row["band"],
            row["center_frequency_mhz"],
        )

        threshold = thresholds.get(
            key
        )

        if threshold is None:
            continue

        delta = row[
            "peak_over_median_db"
        ]

        # Nur oberstes Prozent der gemeinsamen
        # Messperiode betrachten.
        if delta < threshold:
            continue

        result = classify(
            row,
            wifi,
            coverage_by_channel,
        )

        result.update(
            unifi_context(
                unifi_history,
                row["_epoch"],
                result.get(
                    "wifi_channel"
                ),
            )
        )

        results.append({
            "_epoch":
                row["_epoch"],
            "timestamp":
                row["timestamp"],
            "band":
                row["band"],
            "center_mhz":
                row[
                    "center_frequency_mhz"
                ],
            "peak_mhz":
                row[
                    "peak_frequency_mhz"
                ],
            "delta_db":
                delta,
            "threshold_db":
                round(
                    threshold,
                    2,
                ),
            **result,
        })

    mark_recurrent_background(
        results
    )

    mark_recurrent_regions(
        results
    )

    apply_candidate_scores(
        results
    )

    (
        persisted_new,
        current_candidate_count,
    ) = persist_candidate_state(
        results
    )

    counts = defaultdict(int)

    for row in results:
        counts[
            row[
                "classification"
            ]
        ] += 1

    coverage_start_dt = (
        datetime.fromtimestamp(
            coverage_start
        ).astimezone()
    )

    coverage_end_dt = (
        datetime.fromtimestamp(
            coverage_end
        ).astimezone()
    )

    print()
    print(
        f"=== Pluto/WLAN Korrelation V{VERSION} ==="
    )
    print(
        "Analysefenster: "
        f"{ANALYSIS_WINDOW_SECONDS // 3600} Stunden"
    )
    print(
        "Gemeinsame Messperiode: "
        f"{coverage_start_dt.isoformat(timespec='seconds')} "
        "bis "
        f"{coverage_end_dt.isoformat(timespec='seconds')}"
    )
    print(
        f"Pluto gesamt: "
        f"{len(pluto_all)}"
    )
    print(
        f"Pluto im gemeinsamen Zeitraum: "
        f"{len(pluto_rows)}"
    )
    print(
        f"RF-Ausreißer >= P99: "
        f"{len(results)}"
    )

    baseline_start_dt = (
        datetime.fromtimestamp(
            coverage_start
        ).astimezone()
    )

    baseline_end_dt = (
        datetime.fromtimestamp(
            baseline_reference_end
        ).astimezone()
    )

    hybrid_count = sum(
        1
        for source in threshold_sources.values()
        if source == "hybrid_window_max"
    )

    fixed_count = sum(
        1
        for source in threshold_sources.values()
        if source == "fixed_window_max"
    )

    recent_count = sum(
        1
        for source in threshold_sources.values()
        if source == "recent_window_max"
    )

    window_count = sum(
        1
        for source in threshold_sources.values()
        if source == "window_p99"
    )

    print(
        "P99-Referenz: "
        f"{baseline_start_dt.isoformat(timespec='seconds')} "
        "bis "
        f"{baseline_end_dt.isoformat(timespec='seconds')}"
    )
    recent_start_dt = (
        datetime.fromtimestamp(
            baseline_recent_start
        ).astimezone()
    )

    print(
        "P99-Recent: "
        f"{recent_start_dt.isoformat(timespec='seconds')} "
        "bis "
        f"{coverage_end_dt.isoformat(timespec='seconds')}"
    )
    print(
        "P99-Quellen: "
        f"hybrid_window_max={hybrid_count}, "
        f"fixed_window_max={fixed_count}, "
        f"recent_window_max={recent_count}, "
        f"window_p99={window_count}"
    )
    print()
    print(
        "UniFi-Historie: "
        f"{len(unifi_history)} Snapshots"
    )

    if (
        unifi_ap_start is not None
        and unifi_ap_end is not None
    ):
        ap_start_dt = datetime.fromtimestamp(
            unifi_ap_start
        ).astimezone()
        ap_end_dt = datetime.fromtimestamp(
            unifi_ap_end
        ).astimezone()

        print(
            "UniFi AP-Radio Coverage: "
            f"{ap_start_dt.isoformat(timespec='seconds')} "
            "bis "
            f"{ap_end_dt.isoformat(timespec='seconds')} "
            f"({unifi_ap_snapshots} Snapshots)"
        )
    else:
        print(
            "UniFi AP-Radio Coverage: "
            "keine gueltigen Snapshots"
        )

    print()
    print("WLAN-Coverage pro Kanal:")

    for channel in sorted(
        coverage_by_channel
    ):
        start, end = (
            coverage_by_channel[channel]
        )
        start_dt = datetime.fromtimestamp(
            start
        ).astimezone()
        end_dt = datetime.fromtimestamp(
            end
        ).astimezone()

        print(
            f"  CH{channel:<3} "
            f"{start_dt.isoformat(timespec='seconds')} "
            f"bis "
            f"{end_dt.isoformat(timespec='seconds')}"
        )

    print()

    for name in sorted(counts):
        print(
            f"{name:24s} "
            f"{counts[name]}"
        )

    print()
    print(
        "Candidate-Historie: "
        f"{CANDIDATE_FILE} "
        f"(neu={persisted_new})"
    )
    print(
        "Candidate-Current: "
        f"{CANDIDATE_CURRENT_FILE} "
        f"(aktuell={current_candidate_count})"
    )
    print()
    print(
        "=== Kandidaten nach Score ==="
    )

    interesting = [
        row
        for row in results
        if row.get(
            "candidate_score",
            0,
        ) >= 3
    ]

    interesting.sort(
        key=lambda row: (
            row.get(
                "candidate_score",
                0,
            ),
            row["_epoch"],
        ),
        reverse=True,
    )

    unobserved_counts = defaultdict(int)

    for row in results:
        if (
            row["classification"]
            == "wifi_channel_not_observed"
        ):
            unobserved_counts[
                row.get("wifi_channel")
            ] += 1

    if not interesting:
        print(
            "Keine nicht erklärten RF-Kandidaten "
            "im gemeinsamen Messzeitraum."
        )

    for row in interesting[:50]:
        print(
            row["timestamp"],
            row["band"],
            f"score={row.get('candidate_score')}",
            f"level={row.get('candidate_level')}",
            f"peak={row['peak_mhz']:.6f}MHz",
            f"delta={row['delta_db']:.2f}dB",
            f"p99={row['threshold_db']:.2f}dB",
            f"class={row['classification']}",
            f"CH={row.get('wifi_channel')}",
            f"frames={row.get('wifi_frames')}",
            f"ux7_ch={row.get('unifi_clients_channel')}",
            f"ux7_ap_ch={row.get('unifi_ap_radios_channel')}",
            f"ux7_total={row.get('unifi_clients_total')}",
        )

    score_counts = defaultdict(int)

    for row in results:
        score_counts[
            row.get(
                "candidate_level",
                "unknown",
            )
        ] += 1

    full_context = [
        row
        for row in interesting
        if (
            row.get("wifi_frames")
            is not None
            and row.get(
                "unifi_clients_channel"
            ) is not None
            and row.get(
                "unifi_ap_radios_channel"
            ) is not None
        )
    ]

    print()
    print(
        "=== Kandidaten mit vollstaendigem Sensor-Kontext ==="
    )

    if not full_context:
        print(
            "Keine Kandidaten mit gleichzeitigem "
            "Pluto/WLAN/UX7-Client/AP-Kontext."
        )
    else:
        for row in full_context[:50]:
            print(
                row["timestamp"],
                row["band"],
                f"score={row.get('candidate_score')}",
                f"level={row.get('candidate_level')}",
                f"peak={row['peak_mhz']:.6f}MHz",
                f"delta={row['delta_db']:.2f}dB",
                f"CH={row.get('wifi_channel')}",
                f"frames={row.get('wifi_frames')}",
                f"ux7_ch={row.get('unifi_clients_channel')}",
                f"ux7_ap_ch={row.get('unifi_ap_radios_channel')}",
                f"ux7_total={row.get('unifi_clients_total')}",
            )

    print()
    print("=== Candidate-Score Verteilung ===")

    for level in (
        "candidate",
        "interesting",
        "observe",
        "background-like",
        "background",
        "unknown",
    ):
        if score_counts.get(level):
            print(
                f"{level:16s} "
                f"{score_counts[level]}"
            )

    if unobserved_counts:
        print()
        print(
            "=== Nicht überwachte 2.4-GHz-WLAN-Kanäle ==="
        )
        print(
            "Diese Treffer werden nicht als RF-Kandidaten gewertet."
        )

        for channel in sorted(
            unobserved_counts,
            key=lambda value: (
                value is None,
                value if value is not None else 999,
            ),
        ):
            label = (
                f"CH{channel}"
                if channel is not None
                else "unbekannt"
            )
            print(
                f"{label:10s} "
                f"{unobserved_counts[channel]}"
            )



if __name__ == "__main__":
    main()
