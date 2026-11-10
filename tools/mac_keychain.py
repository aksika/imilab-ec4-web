"""macOS Keychain storage for the local EC4 helper.

The browser never stores the camera-cloud password in a cookie or localStorage.
The helper owns it here; the web page is just a normal login form, so the
browser's password manager can also offer to save it if the user wants.
"""
from __future__ import annotations

import json
import keyring

SERVICE = "ec4-imilab"
USER_KEY = "__username__"


def set_credentials(username: str, password: str) -> None:
    keyring.set_password(SERVICE, USER_KEY, username)
    keyring.set_password(SERVICE, username, password)


def get_username() -> str:
    return keyring.get_password(SERVICE, USER_KEY) or ""


def get_credentials() -> tuple[str, str] | None:
    user = get_username()
    if not user:
        return None
    pw = keyring.get_password(SERVICE, user)
    if not pw:
        return None
    return user, pw


def delete_credentials() -> None:
    user = get_username()
    if user:
        try:
            keyring.delete_password(SERVICE, user)
        except Exception:
            pass
    try:
        keyring.delete_password(SERVICE, USER_KEY)
    except Exception:
        pass


def keychain_available() -> bool:
    try:
        return keyring.get_keyring().__class__.__name__ == "Keyring"
    except Exception:
        return False
