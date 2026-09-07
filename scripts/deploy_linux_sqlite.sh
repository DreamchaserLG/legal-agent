#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/legal-demo"
SERVICE_NAME="legal-demo"
APP_USER="legal-demo"
APP_HOST="127.0.0.1"
APP_PORT="8000"
ARCHIVE_PATH=""
INSTALL_SYSTEM_PACKAGES="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --service) SERVICE_NAME="$2"; shift 2 ;;
    --user) APP_USER="$2"; shift 2 ;;
    --host) APP_HOST="$2"; shift 2 ;;
    --port) APP_PORT="$2"; shift 2 ;;
    --archive) ARCHIVE_PATH="$2"; shift 2 ;;
    --skip-apt) INSTALL_SYSTEM_PACKAGES="false"; shift ;;
    -h|--help)
      cat <<EOF
Usage:
  bash scripts/deploy_linux_sqlite.sh [options]

Options:
  --app-dir PATH       Application directory. Default: /opt/legal-demo
  --service NAME       systemd service name. Default: legal-demo
  --user USER          Linux service user. Default: legal-demo
  --host HOST          Uvicorn bind host. Default: 127.0.0.1
  --port PORT          Uvicorn port. Default: 8000
  --archive PATH       Optional tar.gz package to extract before deploying
  --skip-apt           Do not install system packages
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

log() {
  printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"
}

log "Preparing Linux + SQLite deployment"

if [[ "$INSTALL_SYSTEM_PACKAGES" == "true" ]] && command -v apt-get >/dev/null 2>&1; then
  log "Installing system packages"
  $SUDO apt-get update
  $SUDO apt-get install -y python3 python3-venv python3-pip sqlite3 curl
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  log "Creating service user: $APP_USER"
  $SUDO useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

log "Preparing app directory: $APP_DIR"
$SUDO mkdir -p "$APP_DIR"

if [[ -n "$ARCHIVE_PATH" ]]; then
  if [[ ! -f "$ARCHIVE_PATH" ]]; then
    echo "Archive not found: $ARCHIVE_PATH" >&2
    exit 1
  fi
  log "Extracting package: $ARCHIVE_PATH"
  $SUDO tar -xzf "$ARCHIVE_PATH" -C "$APP_DIR" --strip-components=1
fi

if [[ ! -f "$APP_DIR/app/main.py" ]]; then
  echo "app/main.py not found under $APP_DIR. Extract the package first or pass --archive." >&2
  exit 1
fi

$SUDO mkdir -p "$APP_DIR/data" "$APP_DIR/data_archive" "$APP_DIR/data_archive/exports" "$APP_DIR/logs" "$APP_DIR/tmp"

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "Creating .env from .env.sqlite.example"
  $SUDO cp "$APP_DIR/.env.sqlite.example" "$APP_DIR/.env"
fi

log "Ensuring SQLite settings in .env"
$SUDO sed -i "s|^DATABASE_URL=.*|DATABASE_URL=sqlite:///./data/legal_demo.sqlite3|" "$APP_DIR/.env"
$SUDO sed -i "s|^APP_HOST=.*|APP_HOST=$APP_HOST|" "$APP_DIR/.env"
$SUDO sed -i "s|^APP_PORT=.*|APP_PORT=$APP_PORT|" "$APP_DIR/.env"

if grep -q '^SESSION_SECRET=replace-with-a-long-random-string' "$APP_DIR/.env"; then
  SESSION_SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
  $SUDO sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$SESSION_SECRET|" "$APP_DIR/.env"
fi

log "Creating Python virtualenv"
if [[ ! -d "$APP_DIR/venv" ]]; then
  $SUDO python3 -m venv "$APP_DIR/venv"
fi

log "Installing Python dependencies"
$SUDO "$APP_DIR/venv/bin/python" -m pip install --upgrade pip setuptools wheel
$SUDO "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

log "Writing systemd service: $SERVICE_NAME"
$SUDO tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=Legal Demo MVP SQLite service
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python -m uvicorn app.main:app --host \${APP_HOST} --port \${APP_PORT}
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

log "Fixing ownership"
$SUDO chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Starting service"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"

sleep 3
log "Service status"
$SUDO systemctl --no-pager --full status "$SERVICE_NAME" || true

log "Health check"
curl -fsS "http://127.0.0.1:$APP_PORT/health" || {
  echo
  echo "Health check failed. Check logs with:"
  echo "  sudo journalctl -u $SERVICE_NAME -n 100 --no-pager"
  exit 1
}
echo
log "Deployment complete"
echo "Local URL: http://127.0.0.1:$APP_PORT"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
