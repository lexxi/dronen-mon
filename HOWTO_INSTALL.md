# Installation – Drohnen-Monitor

This guide installs the passive monitoring stack on a fresh Debian 13 system.

> The project is intended for passive RF/Wi-Fi observation and analysis. Observe local laws and regulations. The software does not transmit or jam radio signals.

## 1. Hardware

Minimum:
- Debian-capable x86_64 computer
- monitor-mode capable Wi-Fi adapter
- 2 x RTL2832U-compatible SDR receivers

Optional:
- ADALM-Pluto for 2.4/5.8 GHz analysis
- UniFi controller integration for known local Wi-Fi context

## 2. Install Debian packages

```bash
apt update
apt install -y git python3 python3-venv python3-pip \
  iw wireless-tools rfkill usbutils rtl-sdr ieee-data
```

Install the Python packages required by the Wi-Fi sensor:

```bash
apt install -y python3-scapy
```

## 3. Clone the repository

```bash
git clone https://github.com/lexxi/dronen-mon.git /opt/dronen-mon
cd /opt/dronen-mon
```

## 4. Identify the Wi-Fi adapter

```bash
ip link
iw dev
```

The adapter must support monitor mode. Test the supported interface modes with:

```bash
iw list | sed -n '/Supported interface modes:/,/Band/p'
```

The list should contain `monitor`.

## 5. Configure local hardware

Create the local configuration:

```bash
cp /opt/dronen-mon/config/dronen-mon.example /etc/default/dronen-mon
nano /etc/default/dronen-mon
```

Set `DRONEN_WIFI_INTERFACE` to the real Wi-Fi interface.

For the RTL-SDR receivers inspect:

```bash
lsusb
ls -l /sys/bus/usb/devices/
```

Enter the USB topology paths as `SDR_430_USB_PATH` and `SDR_860_USB_PATH`.

## 6. Test RTL-SDR

```bash
rtl_test -t
```

With two receivers attached, verify that both are visible. Stop any DVB kernel driver that has claimed the sticks if `rtl_test` reports that the device cannot be opened.

## 7. Create directories

```bash
mkdir -p /opt/dronen-mon/data/{wifi,sdr/events,sdr/persistent,sdr/baselines,correlation,pluto/events,unifi,archive}
mkdir -p /opt/dronen-mon/config
```

Runtime data below `data/` is deliberately excluded from Git.

## 8. SDR baselines

The SDR monitors expect these baseline files:

```text
/opt/dronen-mon/data/sdr/baselines/sdr_baseline_430_450_peaks.csv
/opt/dronen-mon/data/sdr/baselines/sdr_baseline_860_930_peaks.csv
```

Generate site-specific baselines before enabling continuous SDR monitoring. RF background is location-dependent; a baseline from another installation should not be reused blindly.

For the initial setup, identify the RTL-SDR index with `rtl_test -t`. Then create a capture and analyze it. Example for index 0:

```bash
mkdir -p /opt/dronen-mon/data/sdr/{captures,baselines}

rtl_power -d 0 -f 430M:450M:25k -i 10 -e 1h \
  /opt/dronen-mon/data/sdr/captures/baseline_430_450.csv

python3 /opt/dronen-mon/bin/sdr_analyze.py \
  /opt/dronen-mon/data/sdr/captures/baseline_430_450.csv \
  --output /opt/dronen-mon/data/sdr/baselines/sdr_baseline_430_450_peaks.csv
```

Repeat for 860–930 MHz using the receiver assigned to that range:

```bash
rtl_power -d 1 -f 860M:930M:100k -i 10 -e 1h \
  /opt/dronen-mon/data/sdr/captures/baseline_860_930.csv

python3 /opt/dronen-mon/bin/sdr_analyze.py \
  /opt/dronen-mon/data/sdr/captures/baseline_860_930.csv \
  --output /opt/dronen-mon/data/sdr/baselines/sdr_baseline_860_930_peaks.csv
```

The RTL index is only used for this initial capture. Continuous services use the configured USB topology paths.

## 9. Web interface

```bash
cd /opt/dronen-mon/web
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install gunicorn
```

Test:

```bash
./venv/bin/python app.py
```

The dashboard listens on port 8080.

## 10. Install systemd units

```bash
cp /opt/dronen-mon/systemd/*.service /etc/systemd/system/
cp /opt/dronen-mon/systemd/*.timer /etc/systemd/system/
cp /opt/dronen-mon/web/dronen-web.service /etc/systemd/system/

systemctl daemon-reload
```

Start only the components for which the required hardware and baselines are ready.

Example:

```bash
systemctl enable --now dronen-wifi.service
systemctl enable --now dronen-sdr-430.service
systemctl enable --now dronen-sdr-860.service
systemctl enable --now dronen-correlator.service
systemctl enable --now dronen-web.service
```

Optional Pluto services should only be enabled when an ADALM-Pluto is installed and configured.

## 11. Check status

```bash
systemctl status dronen-wifi dronen-sdr-430 dronen-sdr-860 dronen-correlator dronen-web --no-pager
journalctl -u dronen-wifi -n 100 --no-pager
journalctl -u dronen-sdr-430 -n 100 --no-pager
journalctl -u dronen-sdr-860 -n 100 --no-pager
```

Dashboard:

```text
http://<server-ip>:8080/
```

## 12. Optional UniFi integration

Local UniFi information is intentionally not stored in the repository. The following files are excluded by `.gitignore`:

```text
config/unifi.json
config/unifi_api.key
config/unifi_clients.csv
config/known_devices.csv
config/ignore_bssid.txt
config/ignore_prefixes.txt
```

Never commit API keys, controller addresses, site IDs, real MAC/BSSID inventories or captured measurement data.

## 13. Before reporting problems

Collect:

```bash
ip link
iw dev
lsusb
systemctl --failed
journalctl -u dronen-wifi -n 100 --no-pager
journalctl -u dronen-sdr-430 -n 100 --no-pager
journalctl -u dronen-sdr-860 -n 100 --no-pager
```

Do not publish API keys, private network inventories, MAC/BSSID lists or raw captures in bug reports.
