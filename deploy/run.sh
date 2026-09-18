# run.sh — idempotent installer (runs as root via `sudo -i`).
# Deploys /opt/Lite-support as a dedicated `Lite` system user + systemd service.
# Port auto-select starting at 12345; if occupied, increments until free.
set -euo pipefail

APP_DIR=/opt/Lite-support
DATA_DIR=/opt/Lite-support/data
TARBALL="${1:-/tmp/Lite_support.tar.gz}"
PORT_START=12345
SERVICE=Lite-support
APP_USER=Lite

# 1. dedicated user
id "$APP_USER" >/dev/null 2>&1 || useradd -r -s /bin/bash -d "$APP_DIR" "$APP_USER"

# 2. unpack (wipe app dir only; preserve nothing needed)
rm -rf "$APP_DIR"
mkdir -p "$DATA_DIR/uploads"
tar -xzf "$TARBALL" -C "$APP_DIR"

# 3. python venv + deps
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install --quiet --no-cache-dir -r requirements.txt
.venv/bin/pip install --quiet --no-cache-dir gunicorn || true

# 4. port auto-select (avoid conflict)
port=$PORT_START
while ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${port}\$"; do
  port=$((port+1));
done

cat > "$APP_DIR/.env" <<EOF
PORT=${port}
RZ_DATA=${DATA_DIR}
RZ_SEED_DEMO=1
EOF
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# 5. systemd unit
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=Example Support (helpdesk + knowledge base)
After=network.target

[Service]
Type=simple
User=${APP_USER}
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
sleep 3
if systemctl is-active --quiet ${SERVICE}; then
  echo "SUCCESS port=${port}"
  echo "URL=http://$(hostname -I | awk '{print $1}'):${port}"
else
  echo "FAILED — journal:"
  journalctl -u ${SERVICE} -n 30 --no-pager 2>&1 || true
  exit 1
fi
