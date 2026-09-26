import subprocess


SERVICES = {
    "wifi": "dronen-wifi.service",
    "sdr_430": "dronen-sdr-430.service",
    "sdr_860": "dronen-sdr-860.service",
    "correlator": "dronen-correlator.service",
    "pluto_candidates": "dronen-pluto-correlate.timer",
}


def _state(service):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )

        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def get_service_states():
    return {
        key: {
            "service": service,
            "state": _state(service),
        }
        for key, service in SERVICES.items()
    }
