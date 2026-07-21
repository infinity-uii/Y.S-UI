"""
gateway/routes.py — Unified AI Gateway Blueprint.

Exposes three endpoints:

  POST /api/gateway/chat          — Non-streaming unified chat
  POST /api/gateway/stream        — SSE streaming unified chat
  GET  /api/gateway/providers     — List available providers

All endpoints:
  - Require authentication (session or X-API-Key)
  - Validate the request payload before any key injection
  - Emit structured audit logs
  - Never expose provider API keys in responses or error messages

Standard request payload for chat/stream::

    {
        "provider": "openai",        # required — target provider
        "messages": [                # required — conversation history
            {"role": "system",  "content": "You are a helpful assistant."},
            {"role": "user",    "content": "Hello!"}
        ],
        "model": "gpt-4o",          # optional — falls back to active model
        "stream": false,            # optional — only used by /api/gateway/chat
        "options": {}               # optional — extra provider kwargs (scalars only)
    }
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, jsonify, request, stream_with_context

from gateway.adapter import adapter
from gateway.middleware import audit_log, gateway_auth_required, validate_gateway_payload
from gateway.validator import GatewayRequest

log = logging.getLogger("gateway.routes")

gateway_bp = Blueprint("gateway", __name__, url_prefix="/api/gateway")


# ---------------------------------------------------------------------------
# Helper: resolve model from request or active system default
# ---------------------------------------------------------------------------

def _resolve_model(gw_req: GatewayRequest) -> str:
    if gw_req.model:
        return gw_req.model
    # Fall back to whatever model is active in the main system
    try:
        import agent_system as _sys
        return _sys.get_active_model()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# POST /api/gateway/chat   — non-streaming (and stream=true shorthand)
# ---------------------------------------------------------------------------

@gateway_bp.route("/chat", methods=["POST"])
@gateway_auth_required
@validate_gateway_payload
@audit_log
def gateway_chat():
    """
    Unified, non-streaming AI chat endpoint.

    If ``stream: true`` is set in the payload the request is transparently
    forwarded to the streaming handler so callers don't need to know which
    URL to use.
    """
    gw_req: GatewayRequest = request.gateway_request  # type: ignore[attr-defined]

    if gw_req.stream:
        # Delegate to the SSE generator inline
        return _stream_response(gw_req)

    model = _resolve_model(gw_req)
    ok, text, tokens, provider_used = adapter.chat(
        provider=gw_req.provider,
        messages=gw_req.messages,
        model=model,
        **gw_req.options,
    )

    status = 200 if ok else 502
    return jsonify({
        "ok": ok,
        "reply": text,
        "tokens": tokens,
        "provider": provider_used,
        "model": model,
    }), status


# ---------------------------------------------------------------------------
# POST /api/gateway/stream  — SSE streaming
# ---------------------------------------------------------------------------

@gateway_bp.route("/stream", methods=["POST"])
@gateway_auth_required
@validate_gateway_payload
@audit_log
def gateway_stream():
    """
    Unified SSE streaming chat endpoint.

    Response content-type: ``text/event-stream``
    Each event is a JSON object: ``data: {"text": "chunk"}``
    Terminal event: ``data: [DONE]``
    """
    gw_req: GatewayRequest = request.gateway_request  # type: ignore[attr-defined]
    return _stream_response(gw_req)


def _stream_response(gw_req: GatewayRequest) -> Response:
    model = _resolve_model(gw_req)

    def generate():
        try:
            for chunk in adapter.stream_chat(
                provider=gw_req.provider,
                messages=gw_req.messages,
                model=model,
                **gw_req.options,
            ):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        except Exception as exc:
            log.error("Gateway stream error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering for SSE
        },
    )


# ---------------------------------------------------------------------------
# GET /api/gateway/providers  — list available providers
# ---------------------------------------------------------------------------

@gateway_bp.route("/providers", methods=["GET"])
@gateway_auth_required
def gateway_providers():
    """
    Return the list of providers known to the gateway with their availability.
    Never exposes key values — only presence/absence.
    """
    import os

    key_env_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
    }

    providers = adapter.available_providers()
    # Annotate each provider with whether a key is configured (boolean only)
    for p in providers:
        name = (p.get("name") or "").lower()
        env_var = key_env_map.get(name, "")
        p["key_configured"] = bool(os.environ.get(env_var, "")) if env_var else None

    return jsonify({"ok": True, "providers": providers})
