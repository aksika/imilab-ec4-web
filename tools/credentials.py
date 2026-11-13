"""Operating-system credential storage for the local EC4 helper.

`keyring` selects the native backend: Keychain on macOS, Credential Manager on
Windows, and Secret Service/KWallet on Linux.  The browser never stores the
camera-cloud password in a cookie or localStorage; the helper owns it here.
"""
from __future__ import annotations

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


def store_available() -> bool:
    """Return whether keyring selected a usable backend."""
    try:
        backend = keyring.get_keyring()
        return backend is not None and getattr(backend, "priority", 0) > 0
    except Exception:
        return False


# Backwards-compatible name for older callers.
keychain_available = store_available
