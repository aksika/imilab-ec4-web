#!/bin/zsh
# Double-click to start the pure-Mac web viewer.  No emulator or Android app.
cd "$(dirname "$0")"
exec ./run_web.sh "$@"
