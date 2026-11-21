#!/usr/bin/env bash
# Big Fish CLI installer and updater. Run: curl -fsSL https://raw.githubusercontent.com/Success009/BigFish-cli/main/install.sh | sudo bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
command -v apt-get >/dev/null || { echo "This installer needs a Debian or Ubuntu based system."; exit 1; }
D="${BIGFISH_DIR:-/opt/bigfish}"
U="${SUDO_USER:-root}"
H="$(getent passwd "$U" | cut -d: -f6)"
REPO="Success009/BigFish-cli"

echo "[1/5] System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates xclip wl-clipboard wmctrl x11-utils ffmpeg pulseaudio-utils alsa-utils

echo "[2/5] Google Chrome"
if ! command -v google-chrome >/dev/null; then
  [ "$(dpkg --print-architecture)" = "amd64" ] || { echo "Install Google Chrome yourself (amd64 only here)."; exit 1; }
  curl -fsSL -o /tmp/bigfish-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y -qq /tmp/bigfish-chrome.deb
  rm -f /tmp/bigfish-chrome.deb
fi

echo "[3/5] Big Fish code"
mkdir -p "$D"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$D" --strip-components=1

echo "[4/5] Python packages"
[ -d "$D/venv" ] || python3 -m venv "$D/venv"
"$D/venv/bin/pip" install -q -U pip
"$D/venv/bin/pip" install -q -U -r "$D/requirements.txt"

echo "[5/5] Voice (Piper + Jenny voice)"
A="$D/audio"
mkdir -p "$A/models"
if [ ! -x "$A/piper/piper" ] && [ "$(uname -m)" = "x86_64" ]; then
  curl -fsSL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz | tar -xz -C "$A"
fi
V=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium
[ -f "$A/models/jenny.onnx" ] || curl -fsSL -o "$A/models/jenny.onnx" "$V.onnx"
[ -f "$A/models/jenny.onnx.json" ] || curl -fsSL -o "$A/models/jenny.onnx.json" "$V.onnx.json"

printf '#!/bin/sh\nexec "%s/venv/bin/python3" "%s/main.py" "$@"\n' "$D" "$D" > /usr/local/bin/bf
chmod 755 /usr/local/bin/bf
install -d -o "$U" -g "$U" "$H/gemini_local_agent"
chown -R "$U":"$U" "$D"
echo "Done. Type: bf"
