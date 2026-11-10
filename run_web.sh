#!/bin/zsh
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv and installing dependencies…"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python web/ec4_server.py "$@"
