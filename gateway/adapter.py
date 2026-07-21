"""
gateway/adapter.py — Unified AI Adapter Factory.

This module is the ONLY place where AI provider API keys are injected into
outbound requests. Keys are read directly from environment variables at
call time — they never appear in request payloads, logs, or responses.

Supported providers:  ollama | openai | anthropic | gemini | groq | openai_compatible
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Generator, List, Optional, Tuple

log = logging.getLogger("gateway.adapter")

# ---------------------------------------------------------------------------
# Provider-specific call helpers (thin wrappers — actual HTTP done in
# the main ProviderManager/ProviderBase classes already registered in
# agent_system.py).  We resolve them at call time to avoid circular imports.
# ---------------------------------------------------------------------------

def _get_provider_manager():
    """Return the global ProviderManager from agent_system at call time."""
    try:
        import agent_system as _sys
        return _sys.pm
    except Exception as exc:
        log.error("Could not obtain ProviderManager: %s", exc)
        return None


def _get_key_for(provider_name: str) -> str:
    """
    Read the canonical API key for a provider from environment variables.
    Keys are NEVER cached at module level — read fresh on every call so
    that secrets rotated at runtime are picked up immediately.
    """
    key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
    }
    env_var = key_map.get(provider_name.lower(), "")
    return os.environ.get(env_var, "") if env_var else ""


# ---------------------------------------------------------------------------
# Unified Adapter
# ---------------------------------------------------------------------------

class AIGatewayAdapter:
    """
    Unified AI adapter that routes a standard payload to any supported provider.

    Usage::

        adapter = AIGatewayAdapter()
        ok, text, tokens, provider_used = adapter.chat(
            provider="openai",
            messages=[{"role": "user", "content": "Hello"}],
            model="gpt-4o",
        )
    """

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        model: str = "",
        **options: Any,
    ) -> Tuple[bool, str, int, str]:
        """
        Route a unified chat request to the target provider.

        Returns:
            (ok: bool, text: str, tokens: int, provider_used: str)
        """
        pm = _get_provider_manager()
        if pm is None:
            return False, "Gateway internal error: provider manager unavailable.", 0, provider

        try:
            ok, text, tokens, used = pm.failover_chat(messages, model, provider)
            return ok, text, tokens, used
        except Exception as exc:
            log.error("Gateway adapter chat error [provider=%s]: %s", provider, exc)
            return False, f"Gateway error: {exc}", 0, provider

    # ------------------------------------------------------------ stream_chat
    def stream_chat(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        model: str = "",
        **options: Any,
    ) -> Generator[str, None, None]:
        """
        Route a streaming chat request to the target provider.

        Yields raw text chunks. The caller is responsible for SSE framing.
        """
        pm = _get_provider_manager()
        if pm is None:
            yield "Gateway internal error: provider manager unavailable."
            return

        try:
            prov_obj = pm.resolve_provider(model, provider)
            if prov_obj is None:
                yield f"No provider available for '{provider}'."
                return
            key = pm.get_key(prov_obj.name)
            yield from prov_obj.stream_chat(messages, model, api_key=key, **options)
            pm.report_success(prov_obj.name, key or "")
        except Exception as exc:
            log.error("Gateway adapter stream error [provider=%s]: %s", provider, exc)
            yield f"Gateway error: {exc}"

    # --------------------------------------------------- list_providers
    @staticmethod
    def available_providers() -> List[Dict[str, Any]]:
        """Return the list of configured providers with availability status."""
        pm = _get_provider_manager()
        if pm is None:
            return []
        try:
            return pm.list_providers()
        except Exception:
            return []


# Module-level singleton — instantiated once, stateless.
adapter = AIGatewayAdapter()
