from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Mật khẩu phải có ít nhất 8 ký tự")
    salt = secrets.token_bytes(16)
    n, r, p = 16384, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$", 5)
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(actual, _unb64(digest))
    except Exception:
        return False


def _secret() -> bytes:
    value = os.getenv("VBHC_SESSION_SECRET", "").strip()
    if len(value) < 32:
        raise RuntimeError("VBHC_SESSION_SECRET phải được cấu hình tối thiểu 32 ký tự")
    return value.encode()


def create_session(payload: dict[str, Any], ttl_seconds: int = 86400) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    encoded = _b64(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{sig}"


def parse_session(token: str) -> dict[str, Any] | None:
    try:
        encoded, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(encoded))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(16)
