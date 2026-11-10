"""Local configuration loaded from outside the repository.

The repository contains no account, camera, or gateway secrets.  Put them in
``~/.ec4/config.json`` (mode 0600) or point ``EC4_CONFIG`` at a different file.
See ``config.example.json`` for the shape.

Environment overrides are available for CI or shell use; they take precedence
over the JSON file.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".ec4" / "config.json"


def load() -> dict[str, Any]:
    path = Path(os.environ.get("EC4_CONFIG", str(DEFAULT_PATH))).expanduser()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def get(section: str, key: str, default: Any = "") -> Any:
    env_key = f"EC4_{section.upper()}_{key.upper()}"
    if env_key in os.environ:
        return os.environ[env_key]
    value = (load().get(section) or {}).get(key)
    return default if value is None else value


def camera_ids() -> dict[str, str]:
    """Return ``{display_name: iot_id}`` from the local config."""
    cameras = load().get("cameras") or {}
    result: dict[str, str] = {}
    if isinstance(cameras, dict):
        for name, value in cameras.items():
            if isinstance(value, str):
                result[str(name)] = value
            elif isinstance(value, dict) and value.get("iot_id"):
                result[str(name)] = str(value["iot_id"])
    elif isinstance(cameras, list):
        for index, value in enumerate(cameras, 1):
            if isinstance(value, dict):
                name = value.get("name") or f"CAM{index}"
                iot_id = value.get("iot_id") or ""
            else:
                name, iot_id = f"CAM{index}", str(value)
            if iot_id:
                result[str(name)] = iot_id
    return result


def first_camera_id() -> str:
    cameras = camera_ids()
    return next(iter(cameras.values()), "")


def lan_ip() -> str:
    """Best-effort local LAN address, never a public address."""
    explicit = os.environ.get("EC4_LAN_IP")
    if explicit:
        return explicit
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return "127.0.0.1"


HOSTS_PATH = Path.home() / ".ec4" / "camera_hosts.json"


def host_map() -> dict[str, dict[str, str]]:
    """Return ``{camera_name: {ip, mac, last_seen}}`` for LAN matching."""
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    result: dict[str, dict[str, str]] = {}
    if isinstance(data, dict):
        for name, value in data.items():
            if isinstance(value, dict):
                result[str(name)] = {
                    "ip": str(value.get("ip") or ""),
                    "mac": str(value.get("mac") or "").lower(),
                    "last_seen": str(value.get("last_seen") or ""),
                }
    return result


def save_host_map(mapping: dict[str, dict[str, str]]) -> None:
    HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOSTS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, HOSTS_PATH)


def remember_host(name: str, ip: str, mac: str = "") -> dict[str, dict[str, str]]:
    mapping = host_map()
    mapping[name] = {
        "ip": ip,
        "mac": mac.lower(),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    save_host_map(mapping)
    return mapping
