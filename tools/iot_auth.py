"""Alibaba IoT auth and stream query without the app.

The Alibaba Cloud API Gateway x-ca signature used by this app is HMAC-SHA1
with the app-level seed key.  It was recovered from the SG native signer via a
memory trace and verified against captured signatures; portability across
different app builds/installs is not independently verified.

Sensitive: keep ~/.ec4/iot_creds.json (0600) private.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import ec4_config

HOST = os.environ.get("EC4_IOT_HOST") or ec4_config.get(
    "iot", "host", "eu-central-1.api-iot.aliyuncs.com"
)
APP_KEY = str(ec4_config.get("iot", "app_key", ""))
XCA_KEY = str(ec4_config.get("iot", "xca_key", "")).encode("ascii", "ignore")
CREDS = os.path.expanduser("~/.ec4/iot_creds.json")


def _require_credentials() -> None:
    if not APP_KEY or not XCA_KEY:
        raise RuntimeError(
            "missing IoT gateway credentials; configure ~/.ec4/config.json "
            "(see config.example.json)"
        )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _canonical_headers(headers: dict[str, str]) -> str:
    skip = {"x-ca-signature", "x-ca-signature-headers"}
    keys = sorted(
        k for k in headers if k.startswith("x-ca-") and k not in skip
    )
    return "".join(f"{k}:{headers[k]}\n" for k in keys)


def _send(path: str, body: dict, creds: dict | None = None, extra_headers: dict | None = None) -> dict:
    _require_credentials()
    rid = str(uuid.uuid4())
    payload = dict(body)
    payload["a"] = rid
    payload["id"] = rid
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    md5 = _b64(hashlib.md5(raw).digest())
    date = email.utils.formatdate(usegmt=True)
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    headers = {
        "date": date,
        "x-ca-timestamp": ts,
        "x-ca-nonce": nonce,
        "x-ca-key": APP_KEY,
        "x-ca-version": "1",
        "host": HOST,
        "accept": "application/json; charset=utf-8",
        "content-md5": md5,
        "content-type": "application/octet-stream; charset=utf-8",
        "x-ca-signature-method": "HmacSHA1",
        "user-agent": "ALIYUN-ANDROID-DEMO",
    }
    resource = path + "?x-ca-request-id=" + rid
    headers["x-ca-signature-headers"] = ",".join(
        sorted(
            k for k in headers
            if k.startswith("x-ca-")
            and k not in ("x-ca-signature", "x-ca-signature-headers")
        )
    )
    sts = "\n".join(
        [
            "POST",
            headers["accept"],
            md5,
            headers["content-type"],
            date,
        ]
    ) + "\n" + _canonical_headers(headers) + resource
    sig = _b64(hmac.new(XCA_KEY, sts.encode("utf-8"), hashlib.sha1).digest())
    headers["x-ca-signature"] = sig
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if k.lower() not in {x.lower() for x in headers}})
        sts = "\n".join([
            "POST", headers["accept"], md5, headers["content-type"], date,
        ]) + "\n" + _canonical_headers(headers) + resource
        headers["x-ca-signature"] = _b64(hmac.new(XCA_KEY, sts.encode("utf-8"), hashlib.sha1).digest())
    url = f"https://{HOST}{path}?x-ca-request-id={rid}"
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return {"status": resp.status, "body": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode("utf-8", "replace")}


def load_creds() -> dict:
    if os.path.exists(CREDS):
        with open(CREDS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_creds(creds: dict) -> None:
    os.makedirs(os.path.dirname(CREDS), exist_ok=True)
    tmp = CREDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS)


def refresh(identity: str, refresh_token: str) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.4", "language": "en-US"},
        "d": {"request": {"identityId": identity, "refreshToken": refresh_token}},
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/account/checkOrRefreshSession", body)


def stream_query(iot_id: str, iot_token: str, extra_headers: dict | None = None) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "2.2.0", "language": "en-US", "iotToken": iot_token},
        "d": {
            "relayEncryptType": 0,
            "needDomainName": True,
            "enablePortPredict": True,
            "iotId": iot_id,
            "streamType": 0,
            "clientType": "Android",
            "relayEncrypted": True,
            "forceIFrame": True,
            "enableWebSocket": True,
            "cacheDuration": 2000,
        },
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/vision/customer/stream/query", body, extra_headers=extra_headers)


def thing_properties_get(iot_id: str, iot_token: str) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.2", "language": "en-US", "iotToken": iot_token},
        "d": {"iotId": iot_id},
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/properties/get", body)


def thing_properties_set(iot_id: str, iot_token: str, items: dict) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.2", "language": "en-US", "iotToken": iot_token},
        "d": {"iotId": iot_id, "items": items},
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/properties/set", body)


def start_p2p_streaming(iot_id: str, iot_token: str,
                        stun_url: str, signal_url: str) -> dict:
    """Tell the camera to open its P2P agent for this stream session.

    The TSL service is `StartP2PStreaming(StunUrl, SignalUrl)`; the camera
    must receive the same STUN/signal URLs that the client will use.
    """
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.5", "language": "en-US", "iotToken": iot_token},
        "d": {
            "args": {"StunUrl": stun_url, "SignalUrl": signal_url},
            "identifier": "StartP2PStreaming",
            "iotId": iot_id,
        },
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/service/invoke", body)


def invoke_service(iot_id: str, iot_token: str, identifier: str,
                   args: dict | None = None) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.5", "language": "en-US", "iotToken": iot_token},
        "d": {"args": args or {}, "identifier": identifier, "iotId": iot_id},
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/service/invoke", body)


def start_push_streaming(iot_id: str, iot_token: str, push_url: str,
                         stream_type: int = 0, scheme: int = 0,
                         encrypt_key: str = "", encrypt_type: int = 0,
                         pre_time: int = 0) -> dict:
    """Ask the camera to start its live push (the relay viewer trigger)."""
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.5", "language": "en-US", "iotToken": iot_token},
        "d": {
            "args": {
                "PushUrl": push_url,
                "StreamType": stream_type,
                "Scheme": scheme,
                "EncryptKey": encrypt_key,
                "EncryptType": encrypt_type,
                "PreTime": pre_time,
            },
            "identifier": "StartPushStreaming",
            "iotId": iot_id,
        },
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/service/invoke", body)


def thing_tsl_get(iot_id: str, iot_token: str) -> dict:
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.0", "language": "en-US", "iotToken": iot_token},
        "d": {"iotId": iot_id},
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    return _send("/thing/tsl/get", body)


def ensure_token(creds: dict) -> dict:
    if creds.get("iotToken"):
        return creds
    if creds.get("refreshToken") and creds.get("identityId"):
        res = refresh(creds["identityId"], creds["refreshToken"])
        if res["status"] == 200:
            data = json.loads(res["body"]).get("data") or {}
            creds.update(data)
            save_creds(creds)
    return creds


if __name__ == "__main__":
    creds = load_creds()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    if cmd == "refresh":
        identity = sys.argv[2] if len(sys.argv) > 2 else creds.get("identityId")
        rt = sys.argv[3] if len(sys.argv) > 3 else creds.get("refreshToken")
        if not identity or not rt:
            print("need identityId and refreshToken")
            sys.exit(1)
        res = refresh(identity, rt)
        print(res["status"], res["body"][:1000])
        if res["status"] == 200:
            data = json.loads(res["body"]).get("data") or {}
            merged = dict(creds)
            merged.update(data)
            merged.setdefault("identityId", identity)
            save_creds(merged)
            print("saved", CREDS)
    elif cmd == "stream":
        if not creds.get("iotToken") and creds.get("refreshToken"):
            res = refresh(creds["identityId"], creds["refreshToken"])
            if res["status"] != 200:
                print("refresh failed", res)
                sys.exit(1)
            data = json.loads(res["body"]).get("data") or {}
            creds.update(data)
            save_creds(creds)
        iot = creds.get("iotToken")
        iot_id = sys.argv[2] if len(sys.argv) > 2 else ec4_config.first_camera_id()
        res = stream_query(iot_id, iot)
        print(res["status"], res["body"][:2000])
        if res["status"] == 200 and '"code":200' in res["body"]:
            data=json.loads(res["body"]).get("data") or {}
            open(os.path.expanduser("~/.ec4/stream.json"),"w").write(res["body"])
            os.chmod(os.path.expanduser("~/.ec4/stream.json"),0o600)
            p2p=data.get("p2pInfo") or {}
            print("signalUrl", p2p.get("signalUrl"))
            print("stunUrl", p2p.get("stunUrl"))
            print("relayDecryptKey", data.get("relayDecryptKey"))
    elif cmd in ("wake", "sleep", "props", "tsl"):
        creds = ensure_token(creds)
        iot = creds.get("iotToken")
        iot_id = sys.argv[2] if len(sys.argv) > 2 else ec4_config.first_camera_id()
        if cmd in ("wake", "sleep"):
            res = thing_properties_set(
                iot_id, iot, {"SleepStatus": 1 if cmd == "wake" else 0})
            print(cmd, res["status"], res["body"][:300])
            time.sleep(2)
            res = thing_properties_get(iot_id, iot)
            try:
                data = json.loads(res["body"]).get("data") or {}
            except Exception:
                data = {}
            for key in ("ActiveStatus", "SleepStatus", "BatteryLevel",
                        "BatteryStatus2", "powerSaveMode"):
                if key in data:
                    print(key, data[key])
        elif cmd == "props":
            res = thing_properties_get(iot_id, iot)
            print(res["status"], res["body"][:2000])
        else:
            res = thing_tsl_get(iot_id, iot)
            print(res["status"])
            try:
                data = json.loads(res["body"]).get("data") or {}
            except Exception:
                print(res["body"][:2000])
                data = {}
            path = os.path.expanduser("~/.ec4/tsl.json")
            try:
                raw = json.loads(res["body"])
            except Exception:
                raw = {"raw": res["body"]}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2)
            os.chmod(path, 0o600)
            print("saved", path)
            for svc in data.get("services") or []:
                print("service", svc.get("identifier"), svc.get("name"))
    else:
        print(__doc__)
