#!/bin/bash
# install-web.sh — deploy riêng module AI Incoming/Reply, không ảnh hưởng vbhc-kb.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/venv"
SERVICE=/etc/systemd/system/vbhc-web.service
[ "$EUID" -eq 0 ] || { echo "Chạy bằng sudo/root"; exit 1; }
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --quiet starlette uvicorn python-docx pypdf
cat > "$SERVICE" <<EOF
[Unit]
Description=VBHC AI Incoming Reply Web
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=-/etc/vbhc-web.env
Environment=PYTHONIOENCODING=utf-8
ExecStart=$VENV/bin/python $ROOT/webapp/server.py --host 127.0.0.1 --port 8767
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now vbhc-web
systemctl restart vbhc-web
sleep 1
curl -fsS http://127.0.0.1:8767/healthz
echo
echo "OK: vbhc-web đang chạy tại 127.0.0.1:8767"
