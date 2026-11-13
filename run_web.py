#!/usr/bin/env python3
"""Cross-platform launcher for the EC4 web helper.

Creates ``.venv`` and installs the requirements if necessary, seeds
``~/.ec4/config.json`` on first run, then starts ``web/ec4_server.py``.

Usage:
    python3 run_web.py [server options]
    py -3 run_web.py [server options]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
SERVER = ROOT / "web" / "ec4_server.py"
CONFIG = Path.home() / ".ec4" / "config.json"
EXAMPLE = ROOT / "config.example.json"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_config() -> None:
    if CONFIG.exists():
        return
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    print(f"Created {CONFIG}")


def venv_ready(python: Path) -> bool:
    if not python.exists():
        return False
    try:
        return subprocess.call(
            [str(python), "-c", "import websockets, cryptography, keyring"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) == 0
    except OSError:
        return False


def ensure_venv() -> Path:
    python = venv_python()
    if venv_ready(python):
        return python

    if not python.exists():
        print("Creating .venv…")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])

    print("Installing dependencies…")
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
    )
    return python


def main() -> int:
    ensure_config()
    if Path(sys.prefix).resolve() != VENV.resolve():
        python = ensure_venv()
        return subprocess.call(
            [str(python), str(Path(__file__).resolve()), *sys.argv[1:]]
        )
    return subprocess.call([sys.executable, str(SERVER), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
