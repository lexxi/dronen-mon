#!/usr/bin/env python3

import argparse
import csv
import os
import statistics
from collections import defaultdict


def parse_args():
    p = argparse.ArgumentParser(
        description="Analyse von rtl_power CSV-Dateien mit Peak-Clustering"
    )
    p.add_argument("input")
    p.add_argument("--output")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--window", type=int, default=10)
    p.add_argument("--min-peak-delta", type=float, default=5.0)
    p.add_argument("--min-median-delta", type=float, default=2.0)
    p.add_argument(
        "--cluster-gap",
        type=int,
        default=2,
        help="Maximale Anzahl leerer Bins innerhalb eines Clusters"
    )
    return p.parse_args()


def load_rtl_power_csv(filename):
    values = defaultdict(list)

    with open(filename, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)

        for row in reader:
            if len(row) < 7:
                continue

            try:
                start_hz = float(row[2])
                end_hz = float(row[3])
                step_hz = float(row[4])
            except ValueError:
                continue

            for i, raw in enumerate(row[6:]):
                try:
                    power = float(raw)
                except ValueError:
                    continue

                freq = start_hz + i * step_hz

                if freq <= end_hz + step_hz:
                    values[freq].append(power)

    return values


def calculate_stats(values):
    stats = []

    for freq, measurements in values.items():
        if not measurements:
            continue

        stats.append({
            "frequency_hz": freq,
            "count": len(measurements),
            "median_db": statistics.median(measurements),
            "mean_db": statistics.fmean(measurements),
            "max_db": max(measurements),
            "min_db": min(measurements),
        })

    stats.sort(key=lambda x: x["frequency_hz"])
    return stats


def calculate_local_floor(stats, window):
    for i, item in enumerate(stats):
        start = max(0, i - window)
        end = min(len(stats), i + window + 1)

        neighbours = [
            stats[j]["median_db"]
            for j in range(start, end)
            if j != i
        ]

        if neighbours:
            floor = statistics.median(neighbours)
        else:
            floor = item["median_db"]

        item["local_floor_db"] = floor
        item["median_delta_db"] = item["median_db"] - floor
        item["peak_delta_db"] = item["max_db"] - floor


def candidate_bins(stats, min_peak_delta):
    return [
        x for x in stats
        if x["peak_delta_db"] >= min_peak_delta
    ]


def cluster_bins(stats, candidates, cluster_gap):
    if not candidates:
        return []

    index_by_freq = {
        item["frequency_hz"]: i
        for i, item in enumerate(stats)
    }

    candidate_indices = sorted(
        index_by_freq[x["frequency_hz"]]
        for x in candidates
    )

    clusters = []
    current = [candidate_indices[0]]

    for idx in candidate_indices[1:]:
        if idx - current[-1] <= cluster_gap + 1:
            current.append(idx)
        else:
            clusters.append(current)
            current = [idx]

    clusters.append(current)

    result = []

    for indices in clusters:
        members = [stats[i] for i in indices]

        strongest = max(
            members,
            key=lambda x: x["peak_delta_db"]
        )

        start_hz = members[0]["frequency_hz"]
        end_hz = members[-1]["frequency_hz"]

        weighted_values = []
        weighted_freqs = []

        for m in members:
            weight = max(m["peak_delta_db"], 0.01)
            weighted_values.append(weight)
            weighted_freqs.append(
                m["frequency_hz"] * weight
            )

        center_hz = sum(weighted_freqs) / sum(weighted_values)

        result.append({
            "start_hz": start_hz,
            "end_hz": end_hz,
            "center_hz": center_hz,
            "strongest_hz": strongest["frequency_hz"],
            "bins": len(members),
            "samples": max(x["count"] for x in members),
            "median_db": strongest["median_db"],
            "max_db": strongest["max_db"],
            "local_floor_db": strongest["local_floor_db"],
            "median_delta_db": strongest["median_delta_db"],
            "peak_delta_db": strongest["peak_delta_db"],
        })

    return result


def classify_cluster(cluster, min_median_delta):
    median_delta = cluster["median_delta_db"]
    peak_delta = cluster["peak_delta_db"]

    if median_delta >= min_median_delta:
        return "persistent"

    if peak_delta >= 8:
        return "strong_transient"

    return "transient"


def mhz(hz):
    return hz / 1_000_000.0


def print_clusters(filename, stats, clusters, top, min_median_delta):
    print()
    print(f"Datei: {filename}")
    print(f"Frequenzbins: {len(stats)}")

    if stats:
        print(
            f"Bereich: {mhz(stats[0]['frequency_hz']):.3f} - "
            f"{mhz(stats[-1]['frequency_hz']):.3f} MHz"
        )

    print(f"Signalcluster: {len(clusters)}")
    print()

    print(
        f"{'Center':>12} "
        f"{'Bereich':>25} "
        f"{'ΔMedian':>9} "
        f"{'ΔPeak':>8} "
        f"{'Bins':>5} "
        f"{'Typ':>16}"
    )

    print("-" * 86)

    ordered = sorted(
        clusters,
        key=lambda x: x["peak_delta_db"],
        reverse=True
    )

    for c in ordered[:top]:
        kind = classify_cluster(
            c,
            min_median_delta
        )

        area = (
            f"{mhz(c['start_hz']):.3f}-"
            f"{mhz(c['end_hz']):.3f}"
        )

        print(
            f"{mhz(c['center_hz']):10.3f} MHz "
            f"{area:>25} "
            f"{c['median_delta_db']:9.2f} "
            f"{c['peak_delta_db']:8.2f} "
            f"{c['bins']:5d} "
            f"{kind:>16}"
        )


def write_output(filename, clusters, min_median_delta):
    fields = [
        "center_mhz",
        "start_mhz",
        "end_mhz",
        "strongest_mhz",
        "bins",
        "samples",
        "median_db",
        "max_db",
        "local_floor_db",
        "median_delta_db",
        "peak_delta_db",
        "classification",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fields
        )

        writer.writeheader()

        for c in sorted(
            clusters,
            key=lambda x: x["peak_delta_db"],
            reverse=True
        ):
            writer.writerow({
                "center_mhz":
                    f"{mhz(c['center_hz']):.6f}",
                "start_mhz":
                    f"{mhz(c['start_hz']):.6f}",
                "end_mhz":
                    f"{mhz(c['end_hz']):.6f}",
                "strongest_mhz":
                    f"{mhz(c['strongest_hz']):.6f}",
                "bins":
                    c["bins"],
                "samples":
                    c["samples"],
                "median_db":
                    f"{c['median_db']:.3f}",
                "max_db":
                    f"{c['max_db']:.3f}",
                "local_floor_db":
                    f"{c['local_floor_db']:.3f}",
                "median_delta_db":
                    f"{c['median_delta_db']:.3f}",
                "peak_delta_db":
                    f"{c['peak_delta_db']:.3f}",
                "classification":
                    classify_cluster(
                        c,
                        min_median_delta
                    ),
            })


def main():
    args = parse_args()

    if not os.path.isfile(args.input):
        raise SystemExit(
            f"Datei nicht gefunden: {args.input}"
        )

    values = load_rtl_power_csv(args.input)

    if not values:
        raise SystemExit(
            "Keine gültigen rtl_power-Daten gefunden."
        )

    stats = calculate_stats(values)

    calculate_local_floor(
        stats,
        args.window
    )

    candidates = candidate_bins(
        stats,
        args.min_peak_delta
    )

    clusters = cluster_bins(
        stats,
        candidates,
        args.cluster_gap
    )

    print_clusters(
        args.input,
        stats,
        clusters,
        args.top,
        args.min_median_delta
    )

    if args.output:
        write_output(
            args.output,
            clusters,
            args.min_median_delta
        )

        print()
        print(
            f"Ergebnis geschrieben: {args.output}"
        )


if __name__ == "__main__":
    main()
