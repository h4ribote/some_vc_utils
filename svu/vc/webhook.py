"""Verification of VirtualCrypto webhook deliveries: ed25519 over the timestamp and the body, signed with the application's key pair."""

from __future__ import annotations

import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SIGNATURE_HEADER = "X-Signature-Ed25519"
TIMESTAMP_HEADER = "X-Signature-Timestamp"

# A delivery whose timestamp is further than this from the local clock is refused, which bounds how long a captured delivery can be replayed.
MAX_CLOCK_SKEW_SECONDS = 300

EVENT_PING = 1
EVENT_CLAIM_UPDATE = 2
EVENT_GRANT_DECISION = 3


def verify(public_key_hex: str, signature_hex: str | None, timestamp: str | None, body: bytes, *, now: float | None = None) -> bool:
    """True when signature_hex is the application key's ed25519 signature of timestamp followed by body and the timestamp is within MAX_CLOCK_SKEW_SECONDS."""
    if not signature_hex or not timestamp or not timestamp.isascii() or not timestamp.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode("ascii") + body)
    except (ValueError, InvalidSignature):
        return False
    return True
