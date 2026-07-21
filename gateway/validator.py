"""
gateway/validator.py — Request validation and schema enforcement for the AI Gateway.

All incoming gateway requests are validated here before any AI provider call
or key injection happens. No provider keys ever touch this layer.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_PROVIDERS = {"ollama", "openai", "anthropic", "gemini", "groq", "openai_compatible"}
VALID_ROLES = {"system", "user", "assistant"}
MAX_MESSAGES = 200
MAX_MESSAGE_CHARS = 128_000   # ~100k tokens upper bound
MAX_MODEL_LEN = 128
_SAFE_PROVIDER_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


class GatewayValidationError(ValueError):
    """Raised when an incoming request fails schema validation."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Validated request model (pure Python — no extra dependency)
# ---------------------------------------------------------------------------

class GatewayRequest:
    """
    Parsed and validated gateway request.

    Accepted payload shape::

        {
            "provider": "openai",          # required
            "messages": [                  # required, non-empty list
                {"role": "user", "content": "Hello"}
            ],
            "model": "gpt-4o",            # optional
            "stream": false,              # optional, default false
            "options": {}                 # optional passthrough kwargs
        }
    """

    def __init__(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        model: str = "",
        stream: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.provider = provider
        self.messages = messages
        self.model = model
        self.stream = stream
        self.options: Dict[str, Any] = options or {}

    # ------------------------------------------------------------------
    # Factory / validation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayRequest":
        """Parse and validate a raw request dict. Raises GatewayValidationError on failure."""
        if not isinstance(data, dict):
            raise GatewayValidationError("Request body must be a JSON object.")

        # --- provider ---
        provider = data.get("provider", "")
        if not provider or not isinstance(provider, str):
            raise GatewayValidationError("'provider' is required and must be a non-empty string.")
        provider = provider.strip().lower()
        if not _SAFE_PROVIDER_RE.match(provider):
            raise GatewayValidationError(
                f"'provider' contains invalid characters. "
                f"Use alphanumeric, underscore, or hyphen only (max 64 chars)."
            )

        # --- messages ---
        messages = data.get("messages")
        if not messages or not isinstance(messages, list):
            raise GatewayValidationError("'messages' is required and must be a non-empty list.")
        if len(messages) > MAX_MESSAGES:
            raise GatewayValidationError(
                f"'messages' exceeds maximum allowed length of {MAX_MESSAGES}."
            )
        validated_messages: List[Dict[str, str]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise GatewayValidationError(f"messages[{i}] must be an object.")
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role not in VALID_ROLES:
                raise GatewayValidationError(
                    f"messages[{i}].role must be one of {sorted(VALID_ROLES)}; got '{role}'."
                )
            if not isinstance(content, str):
                raise GatewayValidationError(f"messages[{i}].content must be a string.")
            if len(content) > MAX_MESSAGE_CHARS:
                raise GatewayValidationError(
                    f"messages[{i}].content exceeds {MAX_MESSAGE_CHARS} characters."
                )
            validated_messages.append({"role": role, "content": content})

        # --- model (optional) ---
        model = data.get("model", "")
        if model and not isinstance(model, str):
            raise GatewayValidationError("'model' must be a string.")
        model = (model or "").strip()
        if len(model) > MAX_MODEL_LEN:
            raise GatewayValidationError(f"'model' exceeds {MAX_MODEL_LEN} characters.")

        # --- stream (optional) ---
        stream = data.get("stream", False)
        if not isinstance(stream, bool):
            raise GatewayValidationError("'stream' must be a boolean.")

        # --- options (optional passthrough) ---
        options = data.get("options", {})
        if options and not isinstance(options, dict):
            raise GatewayValidationError("'options' must be an object.")
        # Only allow simple scalar values to prevent injection
        safe_options: Dict[str, Any] = {}
        for k, v in (options or {}).items():
            if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                safe_options[str(k)[:64]] = v

        return cls(
            provider=provider,
            messages=validated_messages,
            model=model,
            stream=stream,
            options=safe_options,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "messages": self.messages,
            "model": self.model,
            "stream": self.stream,
            "options": self.options,
        }
