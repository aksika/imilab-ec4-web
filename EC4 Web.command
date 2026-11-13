#!/bin/zsh
# Double-click to start the local web viewer.
cd "$(dirname "$0")"
exec ./run_web.sh "$@"
