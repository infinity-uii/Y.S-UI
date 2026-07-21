"""
gateway/middleware.py — API Gateway Middleware Layer.

Handles:
  1. Session / API-key authentication (before any key injection)
  2. Request payload validation (schema enforcement via validator.py)
  3. Structured audit logging (provider, model, user identity — never key values)

This middleware runs BEFORE the adapter layer, so if it rejects a request
no provider key is ever read or used.
"""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from flask import jsonify, request, session

from gateway.validator import GatewayRequest, GatewayValidationError

log = logging.getLogger("gateway.middleware")

# ---------------------------------------------------------------------------
# Auth helpers (mirror the auth in agent_system.py without duplication)
# ---------------------------------------------------------------------------

def _get_session_user() -> Optional[str]:
    """Return the username from the active Flask session, or None."""
    return session.get("user")


def _validate_master_api_key(api_key: str) -> bool:
    """
    Check an API key against MASTER_API_KEY env var.
    Reads env var fresh on every call — never cached.
    """
    import os
    master = os.environ.get("MASTER_API_KEY", "")
    return bool(master and api_key == master)


def _resolve_caller() -> Tuple[bool, Optional[str], int]:
    """
    Resolve the authenticated identity from the current request.

    Returns:
        (authenticated: bool, identity: str | None, http_status_if_denied: int)
    """
    # 1. Active browser session
    user = _get_session_user()
    if user:
        return True, user, 200

    # 2. X-API-Key header (or ?api_key= query param)
    api_key = (
        request.headers.get("X-API-Key")
        or request.args.get("api_key", "")
    ).strip()
    if api_key and _validate_master_api_key(api_key):
        return True, "api-key-client", 200

    # 3. Bearer token (OpenAI-compatible style)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
        if bearer and _validate_master_api_key(bearer):
            return True, "bearer-client", 200

    return False, None, 401


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def gateway_auth_required(f: Callable) -> Callable:
    """
    Decorator: authenticate the caller before entering a gateway route.
    Returns 401 JSON if authentication fails.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        ok, identity, status = _resolve_caller()
        if not ok:
            log.warning(
                "Gateway auth denied — ip=%s path=%s",
                request.remote_addr,
                request.path,
            )
            return jsonify({
                "ok": False,
                "error": "Authentication required. "
                         "Provide a valid session cookie or X-API-Key header.",
            }), 401
        # Attach identity to request context for downstream use
        request.gateway_identity = identity  # type: ignore[attr-defined]
        return f(*args, **kwargs)
    return wrapper


def validate_gateway_payload(f: Callable) -> Callable:
    """
    Decorator: parse and validate the JSON body as a GatewayRequest.
    Attaches the validated object to ``request.gateway_request``.
    Returns 400 JSON if validation fails.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        raw = request.get_json(silent=True)
        if raw is None:
            return jsonify({"ok": False, "error": "Request body must be valid JSON."}), 400
        try:
            gw_req = GatewayRequest.from_dict(raw)
        except GatewayValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status
        request.gateway_request = gw_req  # type: ignore[attr-defined]
        return f(*args, **kwargs)
    return wrapper


def audit_log(f: Callable) -> Callable:
    """
    Decorator: emit a structured audit log entry for every gateway call.
    Never logs API key values.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        gw_req: Optional[GatewayRequest] = getattr(request, "gateway_request", None)
        identity: str = getattr(request, "gateway_identity", "unknown")
        log.info(
            "GATEWAY REQUEST — identity=%s provider=%s model=%s messages=%d stream=%s",
            identity,
            gw_req.provider if gw_req else "?",
            gw_req.model if gw_req else "?",
            len(gw_req.messages) if gw_req else 0,
            gw_req.stream if gw_req else "?",
        )
        response = f(*args, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(
            "GATEWAY RESPONSE — identity=%s provider=%s elapsed=%.1fms",
            identity,
            gw_req.provider if gw_req else "?",
            elapsed,
        )
        return response
    return wrapper
