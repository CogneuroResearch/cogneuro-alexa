#!/usr/bin/env bash
# Set up the Pi to run server.py. Idempotent — safe to re-run.
#
#   ./setup_pi.sh
#
# Installs system packages, creates a venv, installs Python deps, and
# downloads a Piper voice. Does not start the server.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/alexa-venv}"
VOICE_DIR="${VOICE_DIR:-$HOME/piper}"
VOICE="${VOICE:-en_US-lessac-medium}"
VOICE_URL_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [ "$(uname -m)" != "aarch64" ]; then
  echo "Warning: this does not look like the Pi (arch $(uname -m)). Continuing anyway."
fi

say "System packages"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev ffmpeg curl

say "Virtualenv at $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip

say "Python dependencies (this is the slow part)"
pip install -r "$HERE/requirements-pi.txt"

say "LLM provider SDK"
PROVIDER="$(grep -E '^LLM_PROVIDER=' "$HERE/.env" 2>/dev/null | cut -d= -f2 | tr -d ' \r' || true)"
PROVIDER="${PROVIDER:-anthropic}"
pip install "$PROVIDER"

say "Piper voice: $VOICE"
mkdir -p "$VOICE_DIR"
for suffix in onnx onnx.json; do
  target="$VOICE_DIR/$VOICE.$suffix"
  if [ -s "$target" ]; then
    echo "  already have $(basename "$target")"
  else
    echo "  downloading $(basename "$target")"
    curl -fL --progress-bar -o "$target" "$VOICE_URL_BASE/$VOICE.$suffix"
  fi
done

say "Checks"
python -c "import faster_whisper, flask, waitress; print('  python deps ok')"
command -v piper >/dev/null && echo "  piper on PATH" || echo "  WARNING: piper not on PATH"
command -v ffmpeg >/dev/null && echo "  ffmpeg ok" || echo "  WARNING: ffmpeg missing"

if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  say "Created .env from the example — you must edit it"
fi

grep -q '^PIPER_MODEL=' "$HERE/.env" 2>/dev/null || \
  printf '\nPIPER_MODEL=%s/%s.onnx\n' "$VOICE_DIR" "$VOICE" >> "$HERE/.env"

cat <<EOF

Done.

Next:
  1. Edit $HERE/.env and set ANTHROPIC_API_KEY
     (PIPER_MODEL has been set to $VOICE_DIR/$VOICE.onnx)
  2. source $VENV/bin/activate
  3. cd $HERE && python server.py

Then from your Mac:
  curl http://cogneuro-pi.local:8080/health
EOF
