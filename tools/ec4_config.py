"""Local configuration loaded from outside the repository.

The repository contains no account, camera, or gateway secrets.  Put them in
``~/.ec4/config.json`` (mode 0600) or point ``EC4_CONFIG`` at a different file.
See ``config.example.json`` for the shape.

Environment overrides are available for CI or shell use; they take precedence
over the JSON file.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".ec4" / "config.json"


def _path() -> Path:
    return Path(os.environ.get("EC4_CONFIG", str(DEFAULT_PATH))).expanduser()


def _write_config(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)


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
        "mac": _norm_mac(mac),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    }
    save_host_map(mapping)
    return mapping


def _norm_mac(mac: str) -> str:
    parts = (mac or "").lower().split(":")
    if len(parts) != 6:
        return (mac or "").lower()
    try:
        return ":".join(f"{int(part, 16):02x}" for part in parts)
    except ValueError:
        return (mac or "").lower()


def _camera_map(data: dict[str, Any]) -> dict[str, Any]:
    """Return the config's camera mapping, converting the legacy list form."""
    cameras = data.get("cameras")
    if isinstance(cameras, list):
        converted: dict[str, str] = {}
        for index, item in enumerate(cameras, 1):
            if isinstance(item, dict) and item.get("iot_id"):
                converted[str(item.get("name") or f"CAM{index}")] = str(item["iot_id"])
            elif isinstance(item, str):
                converted[f"CAM{index}"] = item
        cameras = converted
    return cameras if isinstance(cameras, dict) else {}


def add_camera(name: str, iot_id: str, ip: str = "", mac: str = "") -> dict:
    """Add a camera to the local config; IoT ID comes from the cloud list."""
    name = (name or "").strip()
    iot_id = (iot_id or "").strip()
    if not name:
        raise ValueError("camera name is required")
    if not iot_id:
        raise ValueError("IoT ID is required")
    if len(name) > 80:
        raise ValueError("camera name is too long")
    if len(iot_id) > 80 or not re.fullmatch(r"[A-Za-z0-9_-]+", iot_id):
        raise ValueError("invalid IoT ID")

    data = load()
    cameras = _camera_map(data)
    existing = camera_ids()
    if name in existing:
        raise ValueError("a camera with that name already exists")
    if iot_id in existing.values():
        raise ValueError("that camera is already configured")

    if ip:
        if not ipaddress.ip_address(ip).is_private:
            raise ValueError("camera IP must be a private address")
    cameras[name] = iot_id
    data["cameras"] = cameras
    _write_config(data)
    if ip:
        remember_host(name, ip, mac)
    return {"name": name, "iot_id": iot_id, "ip": ip, "mac": _norm_mac(mac)}


def remove_camera(name: str) -> dict:
    """Remove a camera from the local config and forget its LAN address."""
    name = (name or "").strip()
    if not name:
        raise ValueError("camera name is required")

    data = load()
    cameras = _camera_map(data)
    if name not in cameras:
        raise ValueError("camera is not configured")
    value = cameras.pop(name)
    iot_id = str(value.get("iot_id") or "") if isinstance(value, dict) else str(value)
    data["cameras"] = cameras
    _write_config(data)

    mapping = host_map()
    forgot_host = mapping.pop(name, None) is not None
    if forgot_host:
        save_host_map(mapping)
    return {"name": name, "iot_id": iot_id, "forgot_host": forgot_host}


def _lan_network() -> ipaddress.IPv4Network | None:
    """Return the actual IPv4 network for the interface owning ``lan_ip()``."""
    local = lan_ip()
    try:
        output = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return None
    for line in output.splitlines():
        match = re.search(
            r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", line
        )
        if not match or match.group(1) != local:
            continue
        try:
            mask = ipaddress.IPv4Address(int(match.group(2), 16))
            return ipaddress.IPv4Network(f"{local}/{mask}", strict=False)
        except ValueError:
            return None
    return None


def lan_mismatch(name: str) -> str:
    """Describe a saved LAN address that is outside this Mac's network.

    A sleeping battery camera can be missing from a scan, so this deliberately
    checks only network membership.  It never pings the camera.
    """
    meta = host_map().get((name or "").strip()) or {}
    saved = str(meta.get("ip") or "").strip()
    if not saved:
        return ""
    try:
        local = ipaddress.ip_address(lan_ip())
        camera = ipaddress.ip_address(saved)
    except ValueError:
        return ""
    if not camera.is_private:
        return ""
    if not local.is_private:
        return (
            f"{name} has a saved LAN address {saved}; this Mac is not on that "
            "LAN. Reconnect to the camera network or rescan to rebind it."
        )
    network = _lan_network()
    if network is None:
        network = ipaddress.ip_network(f"{local}/24", strict=False)
    if camera in network:
        return ""
    return (
        f"{name} has a saved LAN address {saved}, which is not on this Mac's "
        f"network ({network.with_prefixlen}). Reconnect the Mac to the camera "
        "LAN or rescan to rebind it."
    )
