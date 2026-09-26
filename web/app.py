#!/usr/bin/env python3

from flask import Flask, render_template, jsonify
import re
from pathlib import Path

from services.wifi_service import (
    get_wifi_summary,
    get_wifi_clients,
    get_wifi_aps,
    get_remote_id_events,
    get_top_observe_clients,
)
from services.sdr_service import get_sdr_summary, get_sdr_events, get_persistent_signals
from services.correlation_service import get_correlations
from services.candidate_service import (
    get_candidates,
    get_candidate_summary,
)
from services.system_service import get_service_states

app = Flask(__name__)


BASE_DIR = Path("/opt/dronen-mon")

COMPONENT_VERSION_FILES = {
    "WLAN Monitor": BASE_DIR / "bin" / "wifi_monitor.py",
    "Remote-ID Decoder": BASE_DIR / "bin" / "remote_id_decoder.py",
    "SDR Monitor": BASE_DIR / "bin" / "sdr_monitor.py",
    "Event Correlator": BASE_DIR / "bin" / "event_correlator.py",
    "Pluto Monitor": BASE_DIR / "bin" / "pluto_monitor.py",
    "Pluto/WLAN Correlator": BASE_DIR / "bin" / "pluto_wifi_correlate.py",
    "History Cleanup": BASE_DIR / "bin" / "cleanup_history.py",
}


def get_component_versions():
    versions = []

    for name, path in COMPONENT_VERSION_FILES.items():
        version = "?"

        try:
            text = path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            match = re.search(
                r'^VERSION\s*=\s*["\']([^"\']+)["\']',
                text,
                flags=re.MULTILINE,
            )

            if match:
                version = match.group(1)
        except Exception:
            pass

        versions.append({
            "name": name,
            "version": version,
        })

    return versions



@app.route("/")
def dashboard():
    services = get_service_states()
    wifi = get_wifi_summary()
    sdr = get_sdr_summary()
    correlations = get_correlations(limit=20)
    remote_id = get_remote_id_events(limit=20)
    top_observe = get_top_observe_clients(limit=10)
    candidate_summary = get_candidate_summary()
    candidates = get_candidates(limit=20)

    return render_template(
        "dashboard.html",
        services=services,
        wifi=wifi,
        sdr=sdr,
        correlations=correlations,
        remote_id=remote_id,
        top_observe=top_observe,
        candidate_summary=candidate_summary,
        candidates=candidates,
    )


@app.route("/wifi")
def wifi():
    return render_template(
        "wifi.html",
        clients=get_wifi_clients(),
        aps=get_wifi_aps(),
    )


@app.route("/sdr")
def sdr():
    return render_template(
        "sdr.html",
        summary=get_sdr_summary(),
        persistent=get_persistent_signals(),
        events=get_sdr_events(limit=100),
    )


@app.route("/correlations")
def correlations():
    return render_template(
        "correlations.html",
        correlations=get_correlations(limit=200),
    )


@app.route("/remote-id")
def remote_id():
    return render_template(
        "remote_id.html",
        events=get_remote_id_events(limit=200),
    )


@app.route("/api/status")
def api_status():
    return jsonify(get_service_states())


@app.route("/api/wifi-summary")
def api_wifi_summary():
    return jsonify(get_wifi_summary())


@app.route("/api/sdr-summary")
def api_sdr_summary():
    return jsonify(get_sdr_summary())


@app.route("/api/wifi-notable")
def api_wifi_notable():
    return jsonify(get_top_observe_clients(limit=10))


@app.route("/api/correlations")
def api_correlations():
    return jsonify(get_correlations(limit=20))


@app.route("/api/remote-id")
def api_remote_id():
    return jsonify(get_remote_id_events(limit=20))


@app.route("/api/candidates")
def api_candidates():
    return jsonify({
        "summary": get_candidate_summary(),
        "rows": get_candidates(limit=20),
    })


@app.route("/documentation")
def documentation():
    return render_template(
        "documentation.html",
        component_versions=get_component_versions(),
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "services": get_service_states(),
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
