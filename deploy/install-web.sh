#!/bin/bash
# Huyền Vũ Văn Bản AI V2 — installer for web/SaaS service.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/venv"
SERVICE=/etc/systemd/system/vbhc-web.service
ENV_FILE=/etc/vbhc-web.env
DATA_DIR=/var/lib/huyen-vu-van-ban-ai

[ "$EUID" -eq 0 ] || { echo "Chạy bằng sudo/root"; exit 1; }
mkdir -p "$DATA_DIR"
chmod 750 "$DATA_DIR"

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --quiet starlette uvicorn python-docx pypdf

if [ ! -f "$ENV_FILE" ]; then
  SESSION_SECRET="$(openssl rand -hex 48)"
  cat > "$ENV_FILE" <<EOF
VBHC_AI_PROVIDER=auto
GEMINI_API_KEY=
OPENAI_API_KEY=
VBHC_GEMINI_MODEL=gemini-3.5-flash-lite
VBHC_OPENAI_MODEL=gpt-5.6-luna
VBHC_SESSION_SECRET=$SESSION_SECRET
VBHC_PLATFORM_ADMIN_EMAILS=
VBHC_AUTO_ACTIVATE_FREE=false
VBHC_ALLOW_ANON_AI=false
VBHC_ALLOW_CONFIDENTIAL_EXTERNAL_AI=false
VBHC_USD_VND=27000
VBHC_COST_RESERVE=1.10
VBHC_MIN_AI_CREDIT=50
VBHC_DEV_DB=$DATA_DIR/huyenvu-vbai.db
VBHC_WEB_HOST=127.0.0.1
VBHC_WEB_PORT=8767
EOF
  chmod 600 "$ENV_FILE"
  echo "[OK] Đã tạo $ENV_FILE và session secret ngẫu nhiên."
  echo "[ACTION] Hãy thêm GEMINI_API_KEY/OPENAI_API_KEY và VBHC_PLATFORM_ADMIN_EMAILS."
else
  chmod 600 "$ENV_FILE"
  echo "[OK] Giữ nguyên $ENV_FILE hiện có."
fi

cat > "$SERVICE" <<EOF
[Unit]
Description=Huyen Vu Van Ban AI V2
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
Environment=PYTHONIOENCODING=utf-8
ExecStart=$VENV/bin/python $ROOT/webapp/server.py --host 127.0.0.1 --port 8767
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vbhc-web
systemctl restart vbhc-web
sleep 1
curl -fsS http://127.0.0.1:8767/healthz
echo
echo "OK: Huyền Vũ Văn Bản AI đang chạy tại 127.0.0.1:8767"
echo "Bootstrap admin: $VENV/bin/python $ROOT/scripts/bootstrap_hv_admin.py --email YOUR_EMAIL"
