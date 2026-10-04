"""Subscriber tokens: what lets a browser or a phone talk to Hermesi's client API as one subscriber.

Minted on your server with your **secret** key, and handed to the app, which sends it with its public key. The format is the one
Hermesi's integration guide gives: ``base64url(payload) + "." + base64url(hmac_sha256(key_hash, base64url(payload)))`` where the
HMAC key is the SHA-256 hex digest of the raw secret key, **not the key itself**. Hermesi stores only that digest, never your
key, which is what lets it verify a signature without ever having the key. Signing with the raw key is the mistake that makes
every token be rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

#: The longest a token may live. The API refuses a later expiry.
MAX_TTL_SECONDS = 3600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_subscriber_token(
    secret_key: str,
    external_id: str,
    environment_id: str,
    *,
    ttl_seconds: int = MAX_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    """A token for ``external_id`` in ``environment_id``, valid for ``ttl_seconds`` (at most an hour).

    ``environment_id`` is the ``env_...`` id of the environment the key belongs to, shown in the dashboard: the key itself does not
    carry it. ``now`` is for tests.
    """
    if not secret_key.startswith("hm_sk_"):
        raise ValueError("secret_key must be a secret key (hm_sk_...), not a public key")
    if not external_id:
        raise ValueError("external_id is required")
    if not environment_id:
        raise ValueError("environment_id is required")
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}")
    issued = time.time() if now is None else now
    payload = {"sub": external_id, "env": environment_id, "exp": int(issued) + ttl_seconds}
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    key_hash = hashlib.sha256(secret_key.encode("utf-8")).hexdigest()
    signature = hmac.new(key_hash.encode("ascii"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(signature)}"
