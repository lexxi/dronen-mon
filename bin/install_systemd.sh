#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/opt/dronen-mon"
SRC_DIR="$BASE_DIR/systemd"
DST_DIR="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
    echo "FEHLER: Bitte als root ausführen."
    exit 1
fi

if [[ ! -d "$SRC_DIR" ]]; then
    echo "FEHLER: systemd-Verzeichnis fehlt: $SRC_DIR"
    exit 1
fi

changed=0
checked=0

echo "=== Drohnen-Monitor systemd Installer ==="
echo

shopt -s nullglob

for src in "$SRC_DIR"/dronen-*.service "$SRC_DIR"/dronen-*.timer; do
    unit="$(basename "$src")"
    dst="$DST_DIR/$unit"

    checked=$((checked + 1))

    if [[ ! -f "$dst" ]]; then
        install -m 0644 "$src" "$dst"
        echo "[INSTALL] $unit"
        changed=$((changed + 1))
        continue
    fi

    if cmp -s "$src" "$dst"; then
        echo "[OK]      $unit"
    else
        install -m 0644 "$src" "$dst"
        echo "[UPDATE]  $unit"
        changed=$((changed + 1))
    fi
done

echo

if [[ $checked -eq 0 ]]; then
    echo "FEHLER: Keine dronen-*.service/.timer Dateien gefunden."
    exit 1
fi

if [[ $changed -gt 0 ]]; then
    systemctl daemon-reload
    echo "systemctl daemon-reload ausgeführt."
else
    echo "Keine Änderungen notwendig."
fi

echo
echo "Geprüft: $checked"
echo "Geändert: $changed"
echo
echo "Hinweis: Dienste werden absichtlich NICHT automatisch neu gestartet."
echo "Bei geänderten Units die betroffenen Dienste anschließend gezielt neu starten."
