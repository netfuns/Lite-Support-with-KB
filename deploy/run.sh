# run.sh — idempotent installer executed as root (sudo -i) on the target host.
# Writes /opt/rankez-support, venv, deps, port auto-select (start 12345), systemd service.
set -euo pipefail

APP_DIR=/opt/rankez-support
DATA_DIR=/opt/rankez-support/data
TARBALL=/tmp/rankez_support.tar.gz
PORT_START=12345
USER="${1:-root}"
SERVICE=rankez-support

# 1. unpack
mkdir -p "$APP_DIR"
if [ -f "$TARBALL" ]; then
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  tar -xzf "$TARBALL" -C "$APP_DIR"
fi
mkdir -p "$DATA_DIR/uploads"
chown -R "$USER":"$USER" "$APP_DIR" 2>/dev/null || chown -R root:root "$APP_DIR"

# 2. python venv + deps
cd "$APP_DIR"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 missing"; exit 1
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
.venv/bin/pip install --quiet --no-cache-dir -r "$APP_DIR/requirements.txt"

# 3. port auto-select (avoid conflict)
port=$PORT_START
while ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${port}\$"; do
  port=$((port+1));
done
echo "PORT=${port}" > "$APP_DIR/.env"
echo "RZ_DATA=${DATA_DIR}" >> "$APP_DIR/.env"
echo "RZ_SEED_DEMO=1" >> "$APP_DIR/.env"
chown "$USER":"$USER" "$APP_DIR/.env" 2>/dev/null || true
chmod 600 "$APP_DIR/.env"

# 4. systemd unit
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=RankEZ Support (helpdesk + KB)
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}/backend
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn app:app --host 0.0.0.0 --port \${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE}
systemctl restart ${SERVICE}
sleep 2
if systemctl is-active --quiet ${SERVICE}; then
  echo "RUNNING on port ${port} — http://$(hostname -I | awk '{print $1}'):${port}"
else
  journalctl -u ${SERVICE} -n 20 --no-pager || systemctl status ${SERVICE}
  exit 1
fi
