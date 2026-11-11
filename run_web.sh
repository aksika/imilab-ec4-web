#!/bin/zsh
set -e
cd "$(dirname "$0")"

# First run: seed the external config with the app-level defaults.
CONFIG="$HOME/.ec4/config.json"
if [ ! -f "$CONFIG" ]; then
  mkdir -p "$HOME/.ec4"
  cp config.example.json "$CONFIG"
  chmod 600 "$CONFIG"
  echo "Created $CONFIG from config.example.json"
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv and installing dependencies…"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python web/ec4_server.py "$@"
