"""Best-effort LAN discovery for the local EC4 helper.

The camera does not advertise mDNS/SSDP, and its Wi-Fi MAC can be a private
(random) address, so this scan combines three signals:

* a fast ping sweep to populate the ARP cache,
* saved camera IP/MAC mappings in ``~/.ec4/camera_hosts.json``,
* camera-like TCP fingerprinting (port 8000 without 1883/MQTT).

Unknown camera-like hosts are reported as candidates; the settings page can
remember one as a configured camera after the first scan.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ec4_config  # noqa: E402

ARP_RE = re.compile(r"\(([0-9.]+)\) at ([0-9a-fA-F:]+)")
IP_NEIGH_RE = re.compile(
    r"^(\d+\.\d+\.\d+\.\d+).*?\blladdr\s+([0-9a-fA-F:]{17})\b",
    re.MULTILINE,
)
PORTS = (80, 443, 554, 1883, 8000, 8080, 8443, 8554, 9000)


def _norm_mac(mac: str) -> str:
    parts = mac.lower().split(":")
    if len(parts) != 6:
        return mac.lower()
    try:
        return ":".join(f"{int(part, 16):02x}" for part in parts)
    except ValueError:
        return mac.lower()


def _parse_ip_neigh(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for ip, mac in IP_NEIGH_RE.findall(output):
        mac = _norm_mac(mac)
        if mac and mac != "ff:ff:ff:ff:ff:ff":
            result[ip] = mac
    return result


def _parse_arp_an(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for ip, mac in ARP_RE.findall(output):
        mac = _norm_mac(mac)
        if mac and mac != "ff:ff:ff:ff:ff:ff":
            result[ip] = mac
    return result


def _arp_table() -> dict[str, str]:
    # Linux: iproute2 neighbor table.  The net-tools arp output is the fallback.
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.run(
                ["ip", "neigh", "show"], capture_output=True, text=True, timeout=4
            ).stdout
        except Exception:
            out = ""
        result = _parse_ip_neigh(out)
        if result:
            return result
    try:
        out = subprocess.run(
            ["arp", "-an"], capture_output=True, text=True, timeout=4
        ).stdout
    except Exception:
        return {}
    return _parse_arp_an(out)


def _ping_cmd(ip: str) -> list[str]:
    if os.name == "nt":
        return ["ping", "-n", "1", "-w", "250", ip]
    if sys.platform == "darwin":
        return ["ping", "-c", "1", "-W", "250", ip]
    # Linux/iputils: -W is a timeout in seconds.
    return ["ping", "-c", "1", "-W", "1", ip]


def _ping(ip: str) -> bool:
    try:
        return subprocess.run(
            _ping_cmd(ip),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).returncode == 0
    except Exception:
        return False


def _open_ports(ip: str, ports: tuple[int, ...] = PORTS) -> list[int]:
    result: list[int] = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        try:
            if sock.connect_ex((ip, port)) == 0:
                result.append(port)
        except Exception:
            pass
        finally:
            sock.close()
    return result


def _network(cidr: str | None) -> ipaddress.IPv4Network:
    if cidr:
        net = ipaddress.ip_network(cidr, strict=False)
    else:
        local = ec4_config.lan_ip()
        cidr = next(
            (
                item["cidr"]
                for item in ec4_config.local_networks()
                if item["ip"] == local
            ),
            "",
        )
        net = ipaddress.ip_network(cidr or f"{local}/24", strict=False)
    if net.version != 4 or not net.is_private:
        raise ValueError("refusing to scan a non-private IPv4 network")
    if net.num_addresses > 512:
        raise ValueError("scan range is too large; use a /23 or smaller")
    return net


def scan(cidr: str | None = None) -> dict:
    started = time.time()
    net = _network(cidr)
    local_ip = ec4_config.lan_ip()
    local_ips = {local_ip}
    local_ips.update(item["ip"] for item in ec4_config.local_networks())
    configured = ec4_config.camera_ids()
    known = ec4_config.host_map()

    by_mac = {
        _norm_mac(str(meta.get("mac") or "")): name
        for name, meta in known.items()
        if meta.get("mac")
    }
    by_ip = {
        str(meta.get("ip") or ""): name
        for name, meta in known.items()
        if meta.get("ip")
    }

    existing = _arp_table()
    hosts = [str(ip) for ip in net.hosts() if str(ip) not in local_ips]
    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = [pool.submit(_ping, ip) for ip in hosts]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass
    arp = _arp_table()

    candidates = {
        ip for ip in set(arp) | set(existing) | set(by_ip)
        if ipaddress.ip_address(ip) in net and ip not in local_ips
    }
    # A saved camera may be asleep and absent from ARP; keep it visible.

    port_map: dict[str, list[int]] = {}
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(_open_ports, ip): ip for ip in candidates}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                port_map[ip] = future.result()
            except Exception:
                port_map[ip] = []

    rows = []
    for ip in sorted(candidates, key=lambda value: ipaddress.ip_address(value)):
        mac = arp.get(ip, "")
        ports = port_map.get(ip, [])
        name = by_mac.get(mac) or by_ip.get(ip) or ""
        likely = bool(name) or (
            8000 in ports and not any(port in ports for port in (80, 443, 1883))
        )
        rows.append({
            "ip": ip,
            "mac": mac,
            "ports": ports,
            "camera": name,
            "likely_camera": likely,
            "kind": "camera" if name else ("possible-camera" if likely else "host"),
        })

    found_names = {row["camera"] for row in rows if row["camera"]}
    configured_rows = [
        {
            "name": name,
            "iot_id": iot_id,
            "known_ip": (known.get(name) or {}).get("ip", ""),
            "seen": name in found_names,
        }
        for name, iot_id in configured.items()
    ]

    return {
        "ok": True,
        "local_ip": local_ip,
        "cidr": str(net),
        "duration": round(time.time() - started, 1),
        "hosts": rows,
        "configured": configured_rows,
    }


def remember(name: str, ip: str, mac: str = "") -> dict:
    if name not in ec4_config.camera_ids():
        raise ValueError("unknown camera name")
    if not ipaddress.ip_address(ip).is_private:
        raise ValueError("camera IP must be private")
    mapping = ec4_config.remember_host(name, ip, _norm_mac(mac))
    return {"ok": True, "hosts": mapping}
