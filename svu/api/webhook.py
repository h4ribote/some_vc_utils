"""POST /vc/webhook: VirtualCrypto's signed deliveries, answered at once and applied by the worker."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from svu.api.state import api_state
from svu.core import webhooks
from svu.vc import webhook as signature

log = logging.getLogger("svu.api.webhook")

router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024


@router.post("/vc/webhook")
async def vc_webhook(request: Request) -> JSONResponse:
    """Answer a PING with {"type": 1}, store any other verified event, and refuse a delivery whose signature does not verify with 401."""
    state = api_state(request)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    sig = request.headers.get(signature.SIGNATURE_HEADER)
    ts = request.headers.get(signature.TIMESTAMP_HEADER)
    key = await state.keys.get(state.engine)
    verified = key is not None and signature.verify(key, sig, ts, body)
    if not verified:
        # The key may have changed since it was cached, so it is read again once before refusing.
        key = await state.keys.get(state.engine, reload=True)
        verified = key is not None and signature.verify(key, sig, ts, body)
    if not verified:
        return JSONResponse({"error": "invalid_signature"}, status_code=401)
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), int):
        return JSONResponse({"error": "invalid_event"}, status_code=400)
    event_type = int(payload["type"])
    if event_type == signature.EVENT_PING:
        return JSONResponse({"type": signature.EVENT_PING})
    async with state.engine.begin() as conn:
        event_id = await webhooks.record(conn, event_type, payload)
    log.info("webhook event stored", extra={"event_id": event_id, "type": event_type})
    return JSONResponse({})
