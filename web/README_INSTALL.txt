Drohnen-Monitor Web GUI V1

1. Verzeichnis anlegen:
   mkdir -p /opt/dronen-mon/web

2. Inhalt dieses Pakets nach /opt/dronen-mon/web kopieren.

3. Venv:
   cd /opt/dronen-mon/web
   python3 -m venv venv
   ./venv/bin/pip install Flask gunicorn

4. Test:
   ./venv/bin/python app.py

   Browser:
   http://<IP-des-dronen-mon>:8080/

5. systemd:
   cp /opt/dronen-mon/web/dronen-web.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now dronen-web

6. Status:
   systemctl status dronen-web --no-pager
   journalctl -f -u dronen-web

Hinweis:
Die GUI ist read-only. Sie liest ausschließlich die bestehenden
CSV/JSONL-Dateien und den systemd-Service-Status.
