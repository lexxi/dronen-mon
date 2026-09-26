# Drohnen-Monitor

Passive experimental drone/RF monitoring stack for Debian/Linux.

The project combines:
- Wi-Fi monitor-mode observation
- passive Remote ID decoding
- RTL-SDR monitoring in configurable frequency ranges
- persistent RF background learning
- Wi-Fi/RF event correlation
- optional ADALM-Pluto 2.4/5.8 GHz analysis
- optional UniFi context
- read-only Flask dashboard

The software is a detection and analysis project. A candidate or correlation is not by itself proof that a drone is present.

## Installation

See [HOWTO_INSTALL.md](HOWTO_INSTALL.md).

## Local data

Runtime captures, learned baselines, device inventories, API credentials and installation-specific configuration must remain local and are excluded from Git where applicable.

## Security / privacy

Before opening issues or sharing logs, remove API keys, internal addresses, MAC/BSSID inventories, device names, locations and raw captures.
