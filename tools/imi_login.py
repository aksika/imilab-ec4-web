"""Imilab username/password login for the local EC4 helper.

The page is a normal login form.  The helper validates the password against
Imilab, stores it in the OS credential store, refreshes the Imilab token, and tries
to exchange the resulting session for Alibaba IoT credentials.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import credentials

HERE = os.path.dirname(os.path.abspath(__file__))
import ec4_config  # noqa: E402
import iot_auth  # noqa: E402
import imi_auth  # noqa: E402

APPID = str(ec4_config.get("imi", "app_id", ""))
SECRET = str(ec4_config.get("imi", "app_secret", "")).encode("ascii", "ignore")
HOSTS = tuple(
    h for h in os.environ.get("EC4_IMI_HOSTS", "").split(",") if h
) or ("gateway.eu-east.imilab.com", "gateway.us-east.imilab.com")
LOGIN_PATH = "oauth/enter/v3/prefix/login"

# Europe OpenAccount gateway.  The global sdk.openaccount.aliyun.com host
# creates a session that the EU IoT gateway cannot see ("session does not
# exist"); this is the host the app actually uses in the EU region.
OA_HOST = os.environ.get("EC4_OA_HOST") or ec4_config.get(
    "openaccount", "host", "living-account.eu-central-1.aliyuncs.com"
)
OA_LOGIN_PATH = "/api/prd/loginbyoauth.json"


def _require_app_credentials() -> None:
    if not APPID or not SECRET:
        raise RuntimeError(
            "missing Imilab app credentials; configure ~/.ec4/config.json "
            "(see config.example.json)"
        )


def _post_json(url: str, body: dict, headers: dict | None = None) -> tuple[int, str]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    h = {**imi_auth._headers(), "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=raw, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=imi_auth._headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        outer = json.loads(base64.urlsafe_b64decode(part))
        inner = outer.get("payload")
        return json.loads(inner) if isinstance(inner, str) else outer
    except Exception:
        return {}


def _imi_uid(token: str) -> str:
    return str(_jwt_payload(token).get("imiUid") or "")


def _tick() -> str:
    last = (0, "")
    for host in HOSTS:
        status, body = _get(f"https://{host}/oauth/login/tick")
        last = (status, body)
        if status == 200:
            try:
                return json.loads(body)["result"]["tick"]
            except Exception:
                continue
    raise RuntimeError(f"tick failed: {last[0]} {last[1][:200]}")


def _rsa_encrypt(public_key_b64: str, text: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    der = base64.b64decode(public_key_b64)
    key = serialization.load_der_public_key(der)
    ciphertext = key.encrypt(text.encode(), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode()


def _sdk_sign(third_uid: str, timestamp: str) -> str:
    _require_app_credentials()
    message = f"{third_uid}+{timestamp}+{APPID}"
    return base64.b64encode(
        hmac.new(SECRET, message.encode(), hashlib.sha256).digest()
    ).decode()


def _login_tick(username: str, password: str) -> dict:
    tick = _tick()
    payload = json.dumps(
        {"account": username, "password": password}, separators=(",", ":")
    )
    body = {
        "s": tick,
        "d": _rsa_encrypt(tick, payload),
        "account": username,
        "countryCode": "",
    }
    last = (0, "")
    for host in HOSTS:
        status, resp = _post_json(f"https://{host}/{LOGIN_PATH}", body)
        last = (status, resp)
        if status != 200:
            continue
        try:
            obj = json.loads(resp)
        except Exception:
            continue
        result = obj.get("result") or obj
        if result.get("account") and not result.get("token"):
            return {"needs_verification": True, "account": result.get("account")}
        if result.get("token") or result.get("refresh_token"):
            return result
        code = obj.get("code")
        raise RuntimeError(obj.get("message") or f"login failed ({code})")
    raise RuntimeError(f"login failed: {last[0]} {last[1][:200]}")


def _sdk_login(imi_uid: str) -> dict:
    ts = str(int(time.time() * 1000))
    body = {
        "appId": APPID,
        "timestamp": ts,
        "thirdUid": imi_uid,
        "sign": _sdk_sign(imi_uid, ts),
    }
    last = (0, "")
    for host in HOSTS:
        status, resp = _post_json(f"https://{host}/oauth/sdk/login", body)
        last = (status, resp)
        if status == 200:
            try:
                obj = json.loads(resp)
                if obj.get("code") == 0 and obj.get("result"):
                    return obj["result"]
            except Exception:
                pass
    raise RuntimeError(f"SDK login failed: {last[0]} {last[1][:200]}")


def _oa_login(auth_code: str, oauth_app_key: str = iot_auth.APP_KEY) -> dict:
    """Exchange an Imilab authCode for an EU OpenAccount session.

    The request is an Alibaba API-gateway POST_FORM call.  The gateway signs
    the *raw* JSON form value; only the transmitted body is URL-encoded.
    """
    request = {
        "oauthPlateform": 23,
        "oauthAppKey": oauth_app_key,
        "authCode": auth_code,
    }
    raw_value = json.dumps(request, separators=(",", ":"))
    body = urllib.parse.urlencode({"loginByOauthRequest": raw_value}).encode()
    date = email.utils.formatdate(usegmt=True)
    headers = {
        "date": date,
        "x-ca-timestamp": str(int(time.time() * 1000)),
        "x-ca-nonce": str(uuid.uuid4()),
        "user-agent": "ALIYUN-ANDROID-DEMO",
        "host": OA_HOST,
        "x-ca-key": iot_auth.APP_KEY,
        "x-ca-version": "1",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "application/json; charset=UTF-8",
        "x-ca-signature-method": "HmacSHA1",
    }
    signed = sorted(
        k for k in headers
        if k.startswith("x-ca-")
        and k not in ("x-ca-signature", "x-ca-signature-headers")
    )
    canonical = "".join(f"{k}:{headers[k]}\n" for k in signed)
    string_to_sign = (
        "POST\n" + headers["accept"] + "\n\n" + headers["content-type"]
        + "\n" + date + "\n" + canonical
        + OA_LOGIN_PATH + "?loginByOauthRequest=" + raw_value
    )
    headers["x-ca-signature-headers"] = ",".join(signed)
    headers["x-ca-signature"] = base64.b64encode(
        hmac.new(iot_auth.XCA_KEY, string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    url = f"https://{OA_HOST}{OA_LOGIN_PATH}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.headers.get("x-ca-error-message") or exc.read().decode(
            "utf-8", "replace"
        )
        raise RuntimeError(f"OpenAccount login failed: {exc.code} {detail}") from exc
    data = payload.get("data") or {}
    result = (data.get("data") or {}).get("loginSuccessResult")
    if data.get("code") != 1 or not result:
        message = data.get("message") or payload.get("errorMsg") or "login failed"
        raise RuntimeError(f"OpenAccount login failed: {message}")
    return result


def _exchange_iot(session_id: str) -> dict:
    """Turn an OpenAccount session id into Alibaba IoT credentials."""
    params = {
        "request": {
            "authCode": session_id,
            "appKey": iot_auth.APP_KEY,
            "accountType": "OA_SESSION",
        }
    }
    body = {
        "b": "1.0",
        "c": {"apiVer": "1.0.4", "language": "en-US"},
        "d": params,
        "params": {"$ref": "$.d"},
        "request": {"$ref": "$.c"},
        "version": "1.0",
    }
    res = iot_auth._send("/account/createSessionByAuthCode", body)
    try:
        obj = json.loads(res["body"])
    except Exception as exc:
        raise RuntimeError(f"IoT exchange returned invalid JSON: {res['body'][:200]}") from exc
    if obj.get("code") != 200:
        message = obj.get("localizedMsg") or obj.get("message") or "exchange failed"
        raise RuntimeError(f"IoT exchange failed: {message}")
    data = obj.get("data") or {}
    if not data.get("iotToken") or not data.get("identityId"):
        raise RuntimeError("IoT exchange returned no credentials")
    return data


def _save_iot(data: dict) -> dict:
    creds = {
        "identityId": data.get("identityId") or data.get("identity"),
        "iotToken": data.get("iotToken"),
        "refreshToken": data.get("refreshToken"),
        "iotTokenExpire": data.get("iotTokenExpire"),
        "refreshTokenExpire": data.get("refreshTokenExpire"),
        "saved_at": int(time.time()),
    }
    if not creds["iotToken"] or not creds["identityId"]:
        raise RuntimeError("IoT exchange returned no credentials")
    iot_auth.save_creds(creds)
    try:
        tokens = imi_auth.load_tokens()
        if tokens and tokens.get("identityId") != creds["identityId"]:
            tokens["identityId"] = creds["identityId"]
            imi_auth.save_tokens(tokens)
    except Exception:
        pass
    return creds


def bootstrap_from_existing_token() -> dict:
    """Create IoT credentials from the saved Imilab session (no password)."""
    imi = imi_auth.load_tokens()
    token = imi.get("token") or ""
    if not token:
        raise RuntimeError("no saved Imilab token; sign in through /settings first")
    imi_uid = _imi_uid(token) or str(imi.get("imiUid") or "")
    if not imi_uid:
        raise RuntimeError("cannot read imiUid from the saved Imilab token")
    sdk = _sdk_login(imi_uid)
    auth_code = sdk.get("authCode") or ""
    if not auth_code:
        raise RuntimeError("Imilab SDK login returned no authCode")
    oa = _oa_login(auth_code)
    session_id = oa.get("sid") or ""
    if not session_id:
        raise RuntimeError("OpenAccount login returned no session id")
    return _save_iot(_exchange_iot(session_id))


def login(username: str, password: str) -> dict:
    username = username.strip()
    if not username or not password:
        raise RuntimeError("username and password are required")
    result = _login_tick(username, password)
    if result.get("needs_verification"):
        return {"ok": True, "needs_verification": True,
                "account": result.get("account")}

    token = result.get("token") or ""
    refresh = result.get("refresh_token") or ""
    imi_uid = str(result.get("uid") or _imi_uid(token))
    if not token:
        raise RuntimeError("login returned no token")

    # Persist the Imilab session.  Keep the existing identity if it is known;
    # the numeric Imilab uid is not the Alibaba IoT identityId.
    old = imi_auth.load_tokens()
    merged = dict(old)
    merged.update({
        "token": token,
        "refresh_token": refresh,
        "imiUid": imi_uid,
        "token_expires_timestamp": result.get("token_expires_timestamp") or 0,
        "refresh_token_expires_timestamp":
            result.get("refresh_token_expires_timestamp") or 0,
    })
    if old.get("identityId"):
        merged["identityId"] = old["identityId"]
    imi_auth.save_tokens(merged)

    # The password is valid; only now put it in the credential store.
    credentials.set_credentials(username, password)

    iot_ok = False
    iot_error = ""
    for oauth_key in (iot_auth.APP_KEY, APPID):
        try:
            sdk = _sdk_login(imi_uid)
            auth_code = sdk.get("authCode") or ""
            if not auth_code:
                raise RuntimeError("Imilab SDK login returned no authCode")
            oa = _oa_login(auth_code, oauth_key)
            _save_iot(_exchange_iot(oa.get("sid") or ""))
            iot_ok = True
            break
        except Exception as exc:
            iot_error = str(exc)

    return {
        "ok": True,
        "username": username,
        "imi_uid": imi_uid,
        "token_expires_timestamp": merged.get("token_expires_timestamp"),
        "iot_ok": iot_ok,
        "iot_error": iot_error,
    }


def status() -> dict:
    imi = imi_auth.load_tokens()
    iot = iot_auth.load_creds()
    username = credentials.get_username()
    return {
        "username": username,
        "has_keychain": bool(username),
        "has_imi_token": bool(imi.get("token")),
        "has_iot_token": bool(iot.get("iotToken")),
        "imi_expires": imi.get("token_expires_timestamp") or 0,
        "iot_identity": iot.get("identityId") or "",
        "iot_saved_at": iot.get("saved_at") or 0,
        "oa_host": OA_HOST,
    }


def refresh_iot() -> dict:
    """Refresh the Alibaba IoT token with the saved IoT refresh token."""
    iot = iot_auth.load_creds()
    identity = str(iot.get("identityId") or "")
    refresh_token = str(iot.get("refreshToken") or "")
    if not identity or not refresh_token:
        raise RuntimeError("no saved IoT refresh token")
    res = iot_auth.refresh(identity, refresh_token)
    try:
        obj = json.loads(res["body"])
    except Exception as exc:
        raise RuntimeError(f"IoT refresh returned invalid JSON: {res['body'][:200]}") from exc
    if obj.get("code") != 200:
        message = obj.get("localizedMsg") or obj.get("message") or "refresh failed"
        raise RuntimeError(f"IoT refresh failed: {message}")
    data = obj.get("data") or {}
    return _save_iot(data)


def ensure_iot_token() -> dict:
    """Refresh the IoT token if it is close to expiry, then re-login if needed."""
    iot = iot_auth.load_creds()
    if iot.get("iotToken"):
        saved = int(iot.get("saved_at") or 0)
        ttl = int(iot.get("iotTokenExpire") or 0)
        if not saved or not ttl or saved + ttl - 300 > time.time():
            return iot
    try:
        return refresh_iot()
    except Exception:
        creds = credentials.get_credentials()
        if not creds:
            raise
        login(*creds)
        return iot_auth.load_creds()


def logout() -> None:
    credentials.delete_credentials()
    for path in (
        os.path.expanduser("~/.ec4/imi_tokens.json"),
        os.path.expanduser("~/.ec4/iot_creds.json"),
    ):
        try:
            os.remove(path)
        except Exception:
            pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EC4 Imilab/IoT login helper")
    parser.add_argument("--bootstrap", action="store_true",
                        help="create IoT credentials from the saved Imilab token")
    parser.add_argument("--refresh", action="store_true",
                        help="refresh the saved IoT credentials")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.bootstrap:
        print(json.dumps(bootstrap_from_existing_token(), indent=2))
    elif args.refresh:
        print(json.dumps(refresh_iot(), indent=2))
    else:
        print(json.dumps(status(), indent=2))
