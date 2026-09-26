Drohnen-Web V1.4 Dashboard Patch

Neu:
- Top Observe+ Clients direkt im Dashboard
- Top 10 aus persistenter client_history.csv
- Felder: Klasse, Score, MAC, Hersteller, RSSI, Kanal, Seen, letzte Sichtung
- automatische Aktualisierung alle 10 Sekunden
- neuer JSON-Endpunkt: /api/wifi-notable
- Dokumentation auf Web V1.4 aktualisiert

Einbau:
  cp -a dronen-web-v1.4-dashboard-patch/. /opt/dronen-mon/web/
  python3 -m py_compile /opt/dronen-mon/web/app.py /opt/dronen-mon/web/services/wifi_service.py
  systemctl restart dronen-web
