#!/usr/bin/env bash
# Install the assistant as a systemd service so it starts at boot and
# survives a closed terminal.
#
#   ./install_service.sh
#
# Idempotent — safe to re-run after changing the unit or the code.

set -euo pipefail

SERVICE="${SERVICE:-alexa}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/alexa-venv}"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
UNIT="/etc/systemd/system/${SERVICE}.service"

if [ ! -x "$VENV/bin/python" ]; then
  echo "No venv at $VENV — run ./setup_pi.sh first." >&2
  exit 1
fi
if [ ! -f "$HERE/.env" ]; then
  echo "No .env in $HERE — the service will start and then fail on a missing key." >&2
fi

echo "==> Writing $UNIT"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Cogneuro Alexa voice assistant
Documentation=https://github.com/CogneuroResearch/cogneuro-alexa
# The LLM API is reached over the network, so wait for a usable route rather
# than merely for the interface to exist.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${HERE}

# server.py loads .env itself via python-dotenv, so no EnvironmentFile here —
# one place for secrets, and it stays out of the unit and out of journalctl.
Environment=HOME=${HOME}
Environment=PYTHONUNBUFFERED=1
# systemd starts with a bare PATH and no activated venv. Piper's CLI lives in
# the venv and is the fallback for synthesis; ffmpeg is used for resampling.
Environment=PATH=${VENV}/bin:/usr/local/bin:/usr/bin:/bin

ExecStart=${VENV}/bin/python ${HERE}/server.py

# Whisper takes ~15s to load from cold, and longer the first time it fetches
# the model. Without this systemd would give up and restart mid-load, forever.
TimeoutStartSec=180

Restart=on-failure
RestartSec=5
# If it fails five times in two minutes something is genuinely wrong; stop
# rather than hammering the Anthropic API from a crash loop.
StartLimitBurst=5
StartLimitIntervalSec=120

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}.service"
sudo systemctl restart "${SERVICE}.service"

sleep 3
echo
sudo systemctl --no-pager --lines=0 status "${SERVICE}.service" || true

cat <<EOF

Installed.

  follow the log     journalctl -u ${SERVICE} -f
  restart it         sudo systemctl restart ${SERVICE}
  stop it            sudo systemctl stop ${SERVICE}
  after a code push  sudo systemctl restart ${SERVICE}

It will take about 15 seconds to answer the first request while Whisper
loads. Check it is up with:

  curl -s http://localhost:8080/health
EOF
