"""Signed client-held public chat/context. Never log tokens or trust unsigned facts."""
import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException


def _key(settings):
    secret = settings.gateway_internal_secret.strip()
    if len(secret) < 32 or secret == "hs-internal-gateway-secret-dev-2026":
        raise HTTPException(503, "Guest context signing is not securely configured.")
    return hmac.digest(secret.encode(), b"homespace:guest-context:v1", "sha256")


def encode_state(state: dict, settings) -> str:
    state = {**state, "expires": int(time.time()) + settings.guest_chat_context_ttl_seconds}
    payload = base64.urlsafe_b64encode(json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=str).encode()).decode().rstrip("=")
    signature = hmac.new(_key(settings), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def decode_state(token: str, owner: str, settings) -> dict:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(_key(settings), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        state = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if state["owner"] != owner or state["expires"] <= time.time():
            raise ValueError("owner/expiry")
        return state
    except (ValueError, KeyError, TypeError):
        raise HTTPException(403, "Invalid or expired guest context.") from None
