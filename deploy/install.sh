#!/usr/bin/env bash
# Install or upgrade Agile Forecast on Debian/Ubuntu (e.g. a Proxmox LXC). Run as root from anywhere:
#   bash deploy/install.sh
set -euo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8  # the container may lack the locale forwarded by SSH

APP_DIR=/opt/agile-forecast
DATA_DIR=/var/lib/agile-forecast
ENV_FILE=/etc/agile-forecast.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Please run as root (sudo bash deploy/install.sh)"; exit 1; }

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 rsync git ca-certificates tzdata >/dev/null

echo "==> Creating service user and directories"
id agile &>/dev/null || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin agile
mkdir -p "$APP_DIR" "$DATA_DIR"

echo "==> Copying application to $APP_DIR"
# A git clone in $APP_DIR is used in place; a copy elsewhere is synced into $APP_DIR.
[[ "$SRC_DIR" == "$APP_DIR" ]] || rsync -a --delete --exclude .venv --exclude data --exclude __pycache__ --exclude .git "$SRC_DIR"/ "$APP_DIR"/

echo "==> Creating Python virtualenv (this can take a few minutes)"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Writing default config to $ENV_FILE"
  cat > "$ENV_FILE" <<CONF
AGILE_DATA_DIR=$DATA_DIR
AGILE_HOST=0.0.0.0
AGILE_PORT=8000
# Leave blank to auto-detect the latest Agile product, or pin e.g. AGILE-24-10-01
AGILE_PRODUCT=
AGILE_HISTORY_DAYS=365
AGILE_KEEP_RUNS_DAYS=180
CONF
fi

chown -R root:root "$APP_DIR"
chown -R agile:agile "$DATA_DIR"

echo "==> Installing systemd units"
install -m 644 "$APP_DIR"/deploy/agile-forecast.service /etc/systemd/system/
install -m 644 "$APP_DIR"/deploy/agile-forecast-update.service /etc/systemd/system/
install -m 644 "$APP_DIR"/deploy/agile-forecast-update.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agile-forecast-update.timer
systemctl enable agile-forecast.service
systemctl restart agile-forecast.service

if [[ ! -f "$DATA_DIR/agile.db" ]]; then
  echo "==> First run: downloading a year of history and training the model (1-3 minutes)"
  systemctl start agile-forecast-update.service || {
    echo "First update failed; see: journalctl -u agile-forecast-update -n 50"; exit 1; }
fi

source "$ENV_FILE"
IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Open http://$IP:$AGILE_PORT/"
echo "  Logs:        journalctl -u agile-forecast -f   |   journalctl -u agile-forecast-update"
echo "  Next update: systemctl list-timers agile-forecast-update.timer"
