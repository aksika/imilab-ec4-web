"""Imilab Home auth without the app.

The app stores an Imilab JWT access token and a 14-day refresh token in
SharedPreferences (`IMI_sp_key_Name.xml`).  The refresh endpoint accepts the
refresh token as a GET parameter and does NOT require the white-box
`X-IMILAB-SIGN` headers, so the Mac can keep the session alive indefinitely.

Usage:
    python tools/imi_auth.py refresh
    python tools/imi_auth.py devices
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ec4_config
import iot_auth

ADB = os.environ.get("EC4_ADB") or shutil.which("adb") or "adb"
PREFS = "/data/data/com.chuangmi.imihome/shared_prefs/IMI_sp_key_Name.xml"
TOKEN_FILE = os.path.expanduser("~/.ec4/imi_tokens.json")
APP_ID = str(ec4_config.get("imi", "app_id", ""))
IMI_APP_ID = "com.chuangmi.imihome"
BASE_HOSTS = tuple(
    h for h in os.environ.get("EC4_IMI_HOSTS", "").split(",") if h
) or ("gateway.eu-east.imilab.com", "gateway.us-east.imilab.com")
# Read once from the app (com.imi.netkit.e.getIdentityId); stable for the account.
DEFAULT_IDENTITY = str(ec4_config.get("imi", "identity_id", ""))


def _adb_cat(path: str) -> str:
    return subprocess.check_output([ADB, "shell", "su", "0", "cat", path], text=True)


def load_tokens_from_app() -> dict:
    xml = _adb_cat(PREFS)
    m = re.search(r'name="imi_sp_user_token_data_key">(.*?)</string>', xml, re.S)
    if not m:
        raise RuntimeError("token blob not found in app prefs")
    return json.loads(m.group(1).replace("&quot;", '"'))


def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not data.get("identityId"):
            data["identityId"] = DEFAULT_IDENTITY
            save_tokens(data)
        return data
    data = load_tokens_from_app()
    data.setdefault("identityId", DEFAULT_IDENTITY)
    save_tokens(data)
    return data


def save_tokens(data: dict) -> None:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def _headers(access_token: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.8",
        "APP-ID": APP_ID,
        "IMI-APP-ID": IMI_APP_ID,
        "APP-VERSION": "2.9.2",
        "APP-VERSION-CODE": "128",
        "Client-OS": "android",
        "User-Agent": "00000000-2127-1a92-ffff-ffffef05ac4a_Android15_sdk_gphone64_arm64_2.9.2_en-US_android",
    }
    if access_token:
        h["X-IMILAB-TOKEN"] = access_token
    return h


def _request(url: str, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _request_post(url: str, headers: dict, payload: dict) -> tuple[int, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=raw, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def refresh() -> dict:
    data = load_tokens()
    rt = data["refresh_token"]
    last = None
    for host in BASE_HOSTS:
        url = f"https://{host}/oauth/sdk/refreshToken?refreshToken=" + urllib.parse.quote(rt)
        status, body = _request(url, _headers())
        last = (status, body)
        if status == 200:
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            result = payload.get("result") or {}
            if result.get("token") and result.get("refresh_token"):
                merged = dict(data)
                merged.update(result)
                save_tokens(merged)
                return merged
    raise RuntimeError(f"refresh failed: {last}")


def auth_headers(refresh_if_needed: bool = True) -> dict:
    data = load_tokens()
    exp = int(data.get("token_expires_timestamp") or 0)
    if refresh_if_needed and exp and exp - time.time() * 1000 < 3600_000:
        data = refresh()
    return _headers(data.get("token"))


def devices() -> dict:
    data = load_tokens()
    identity = data.get("identityId") or data.get("identity_id") or DEFAULT_IDENTITY
    headers = auth_headers()
    if identity:
        headers["X-IMILAB-IDENTITYID"] = identity
    # The cloud device list is an "Ali" endpoint: it needs the raw Alibaba
    # IoT token in Authorization, not the Imilab token.
    ali_token = iot_auth.load_creds().get("iotToken") or ""
    if ali_token:
        headers["Authorization"] = ali_token
    url = "https://gateway.eu-east.imilab.com/api/app_device/devices"
    status, body = _request_post(url, headers, {"pageNo": 1, "pageSize": 100})
    return {"status": status, "body": body[:20000]}


def device_list() -> list[dict]:
    """Return the account's cloud devices (including iotId and product info)."""
    result = devices()
    if result["status"] != 200:
        raise RuntimeError(f"device list HTTP {result['status']}")
    try:
        payload = json.loads(result["body"])
    except ValueError as exc:
        raise RuntimeError("device list returned invalid JSON") from exc
    items = payload.get("result")
    if isinstance(items, dict):
        items = items.get("devices") or items.get("list") or []
    if not isinstance(items, list):
        raise RuntimeError(
            payload.get("message") or "device list returned no devices"
        )
    return [item for item in items if isinstance(item, dict) and item.get("iotId")]


def cloud_cameras() -> list[dict]:
    """Return camera-like cloud devices with a stable iotId."""
    cameras: list[dict] = []
    for item in device_list():
        category = str(item.get("categoryKey") or "").lower()
        product = str(item.get("productName") or "")
        if not (
            category in ("camera", "doorbell")
            or "EC4" in product.upper()
            or "CAMERA" in product.upper()
        ):
            continue
        cameras.append({
            "iot_id": item.get("iotId"),
            "name": item.get("nickName") or item.get("deviceName") or item.get("iotId"),
            "device_name": item.get("deviceName") or "",
            "product_name": product,
            "product_key": item.get("productKey") or "",
            "category": category,
            "status": item.get("status"),
            "net_type": item.get("netType") or "",
            "parent_iot_id": item.get("parentIotId") or "",
        })
    cameras.sort(key=lambda cam: (cam["name"] or "").lower())
    return cameras


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    if cmd == "refresh":
        d = refresh()
        print(json.dumps({k: d.get(k) for k in (
            "token_expires_timestamp", "refresh_token_expires_timestamp")}, indent=2))
        print("token", (d.get("token") or "")[:40] + "...")
    elif cmd == "devices":
        print(json.dumps(devices(), indent=2)[:4000])
    elif cmd == "show":
        d = load_tokens()
        print(json.dumps({k: d.get(k) for k in (
            "token_expires_timestamp", "refresh_token_expires_timestamp")}, indent=2))
    else:
        print(__doc__)
