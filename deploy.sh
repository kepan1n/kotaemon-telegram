#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/kotaemon-telegram-bridge"
SERVICE_NAME="kotaemon-telegram-bridge"
RUN_USER="${1:-$USER}"

echo "[1/8] Preparing app dir: ${APP_DIR}"
sudo mkdir -p "$APP_DIR"
sudo chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "[2/8] Installing OS deps for playwright chromium"
sudo apt-get update
sudo apt-get install -y \
  python3-venv \
  libatk1.0-0t64 \
  libatk-bridge2.0-0t64 \
  libatspi2.0-0t64 \
  libxcomposite1 \
  libxdamage1 \
  libxfixes3 \
  libxrandr2 \
  libgbm1 \
  libasound2t64

echo "[3/8] Sync files"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'bot.log' \
  --exclude 'state.db' \
  ./ "$APP_DIR"/

echo "[4/8] Python venv + deps"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -U pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[5/8] Prepare writable runtime dirs"
sudo mkdir -p "$APP_DIR/.hf/hub" "$APP_DIR/.pw-browsers"
sudo chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/.hf" "$APP_DIR/.pw-browsers"

echo "[6/8] Install playwright chromium"
sudo -u "$RUN_USER" -H env PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.pw-browsers" \
  "$APP_DIR/.venv/bin/python" -m playwright install chromium

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "[7/8] Creating .env from example (edit it before first start)"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

echo "[8/8] Installing systemd unit + restart"
TMP_UNIT="/tmp/${SERVICE_NAME}.service"
cp "$APP_DIR/systemd/${SERVICE_NAME}.service" "$TMP_UNIT"
sed -i "s/%i/${RUN_USER}/g" "$TMP_UNIT"
sudo cp "$TMP_UNIT" "/etc/systemd/system/${SERVICE_NAME}.service"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Done. Status:"
sudo systemctl --no-pager -l status "$SERVICE_NAME" || true

echo "Logs: journalctl -u ${SERVICE_NAME} -f"
