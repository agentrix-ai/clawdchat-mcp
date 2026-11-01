"""Standard Webhooks signing (https://www.standardwebhooks.com/)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Iterable

SECRET_PREFIX = "whsec_"
MIN_KEY_BYTES = 24
MAX_KEY_BYTES = 64


class InvalidSecret(ValueError):
    pass


def decode_secret(secret: str) -> bytes:
    """``whsec_<base64>`` -> key bytes; must decode to 24-64 bytes."""
    if not isinstance(secret, str) or not secret.startswith(SECRET_PREFIX):
        raise InvalidSecret("secret must start with whsec_")
    try:
        key = base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSecret("secret is not valid base64") from exc
    if not MIN_KEY_BYTES <= len(key) <= MAX_KEY_BYTES:
        raise InvalidSecret("secret must decode to 24-64 bytes")
    return key


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    key = decode_secret(secret)
    to_sign = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(key, to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def sign_all(secrets: Iterable[str], msg_id: str, timestamp: int, body: bytes) -> str:
    """Space-separated signatures (secret rotation window)."""
    return " ".join(sign(s, msg_id, timestamp, body) for s in secrets)


def verify(secret: str, msg_id: str, timestamp: int, body: bytes, header: str) -> bool:
    expected = sign(secret, msg_id, timestamp, body)
    return any(hmac.compare_digest(expected, part) for part in (header or "").split())


def dump_body(payload: Any) -> bytes:
    """Serialize once; sign and send these exact bytes."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
