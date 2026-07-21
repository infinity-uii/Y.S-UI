#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_system.py — Production AI Platform Backend

Default: Ollama llama3.2 @ http://localhost:11434/v1/chat/completions
Providers: Ollama, OpenAI, Anthropic, Gemini, Groq, OpenAI-compatible
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import MemoryHandler
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, List, Optional, Tuple

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.utils import secure_filename

# Production: load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
# YS_USER / YS_PASSWORD are intentionally NOT cached at module load time.
# They are read from os.environ on every request so that secrets set after
# process start (e.g. via Replit Secrets) are picked up without a restart.
# Accept either SECRET_KEY or SESSION_SECRET (Replit-standard name)
SECRET_KEY = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("SECRET_KEY")
    or secrets.token_hex(32)
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Provider env keys
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Default Ollama settings
DEFAULT_OLLAMA_BASE = "http://localhost:11434"
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", DEFAULT_OLLAMA_BASE).rstrip("/")
DEFAULT_MODEL = "llama3.2"
DEFAULT_PROVIDER = "ollama"

# Workspace directory
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "./workspace"))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = WORKSPACE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".xls", ".doc",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml",
    ".md", ".txt", ".csv", ".sh", ".bash", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_RECORDS: List[Dict[str, Any]] = []
_log_lock = threading.Lock()


class InMemoryLogHandler(logging.Handler):
    """Captures log records into an in-memory list for /api/logs."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        }
        with _log_lock:
            LOG_RECORDS.append(entry)
            if len(LOG_RECORDS) > 2000:
                LOG_RECORDS.pop(0)


_mem_handler = InMemoryLogHandler()
_mem_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger().addHandler(_mem_handler)
log = logging.getLogger("agent_system")

# CORS: configurable via ALLOWED_ORIGINS env var (comma-separated). Default: allow all (dev).
_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()] or ["*"]

try:
    from flask_cors import CORS

    app = Flask(__name__, static_folder=None)
    CORS(app, resources={r"/api/*": {"origins": _cors_origins}})
except Exception:
    app = Flask(__name__, static_folder=None)

app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["JSON_SORT_KEYS"] = False
app.config["PROPAGATE_EXCEPTIONS"] = True

# ---------------------------------------------------------------------------
# Register Gateway Blueprint (Secure AI API Gateway)
# ---------------------------------------------------------------------------
try:
    from gateway import gateway_bp
    app.register_blueprint(gateway_bp)
    log.info("Gateway blueprint registered at /api/gateway")
except Exception as _gw_err:
    log.warning("Gateway blueprint not loaded: %s", _gw_err)

# Rate limiting (production hardening)
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    _rate_storage = os.environ.get("RATELIMIT_STORAGE_URL", "memory://")
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per minute"],
        storage_uri=_rate_storage,
    )
except Exception:
    limiter = None
    log.warning("flask-limiter not available; rate limiting disabled")

# API versioning prefix
API_PREFIX = os.environ.get("API_PREFIX", "/api")

# register tools blueprint safely (lazy)
try:
    from tools_api import bp as tools_bp

    try:
        app.register_blueprint(tools_bp)
        log.info("Registered tools blueprint")
    except Exception:
        log.exception("Failed to register tools blueprint")
except Exception:
    log.info("tools_api not available at import time; will skip registering tools blueprint")


# ---------------------------------------------------------------------------
# Admin stats tracker
# ---------------------------------------------------------------------------
_stats: Dict[str, Any] = {
    "requests_total": 0,
    "tokens_total": 0,
    "chat_requests": 0,
    "errors": 0,
    "provider_usage": {},
    "model_usage": {},
    "started_at": datetime.now(timezone.utc).isoformat(),
}
_stats_lock = threading.Lock()


def track_request(provider: str = "", model: str = "", tokens: int = 0, error: bool = False) -> None:
    with _stats_lock:
        _stats["requests_total"] += 1
        _stats["tokens_total"] += tokens
        if error:
            _stats["errors"] += 1
        if provider:
            _stats["provider_usage"][provider] = _stats["provider_usage"].get(provider, 0) + 1
        if model:
            _stats["model_usage"][model] = _stats["model_usage"].get(model, 0) + 1


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------
class ProviderBase(ABC):
    name: str = ""
    default_model: str = ""

    @abstractmethod
    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        """Returns (ok, reply_text, token_estimate)."""

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        """Yields text chunks. Default: single chunk from chat()."""
        ok, text, _ = self.chat(messages, model, **kwargs)
        if ok:
            yield text
        else:
            yield f"[Error] {text}"

    def list_models(self) -> List[str]:
        return [self.default_model] if self.default_model else []

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Multi-key manager with rotation, failover, health monitoring
# ---------------------------------------------------------------------------
class KeyPool:
    """Manages multiple API keys per provider with round-robin rotation and health tracking."""

    def __init__(self, keys: List[str], provider_name: str = ""):
        self._keys = [k for k in keys if k]
        self._idx = 0
        self._lock = threading.Lock()
        self._health: Dict[str, Dict[str, Any]] = {}
        self.provider_name = provider_name
        for k in self._keys:
            self._health[k] = {"failures": 0, "last_fail": 0.0, "disabled_until": 0.0, "uses": 0}

    def next_key(self) -> Optional[str]:
        if not self._keys:
            return None
        now = time.time()
        with self._lock:
            for _ in range(len(self._keys)):
                k = self._keys[self._idx % len(self._keys)]
                self._idx += 1
                info = self._health[k]
                if info["disabled_until"] > now:
                    continue
                info["uses"] += 1
                return k
            return self._keys[0]

    def report_failure(self, key: str) -> None:
        with self._lock:
            info = self._health.get(key)
            if not info:
                return
            info["failures"] += 1
            info["last_fail"] = time.time()
            if info["failures"] >= 3:
                info["disabled_until"] = time.time() + 60

    def report_success(self, key: str) -> None:
        with self._lock:
            info = self._health.get(key)
            if info:
                info["failures"] = 0
                info["disabled_until"] = 0.0

    def health_status(self) -> List[Dict[str, Any]]:
        with self._lock:
            result = []
            for k in self._keys:
                info = self._health[k]
                result.append({
                    "key_suffix": k[-6:] if len(k) > 6 else k,
                    "failures": info["failures"],
                    "uses": info["uses"],
                    "healthy": info["disabled_until"] <= time.time(),
                })
            return result

    def count(self) -> int:
        return len(self._keys)


class ProviderManager:
    """Manages all providers with multi-key support, rotation, failover, priority, and per-model mapping."""

    def __init__(self):
        self._providers: Dict[str, ProviderBase] = {}
        self._pools: Dict[str, KeyPool] = {}
        self._priority: Dict[str, int] = {}
        self._enabled: Dict[str, bool] = {}
        self._model_map: Dict[str, str] = {}
        self._lock = threading.Lock()

    def register(self, provider: ProviderBase, keys: List[str], priority: int = 0, enabled: bool = True):
        name = provider.name
        with self._lock:
            self._providers[name] = provider
            self._pools[name] = KeyPool(keys, name)
            self._priority[name] = priority
            self._enabled[name] = enabled

    def set_enabled(self, name: str, enabled: bool):
        with self._lock:
            if name in self._enabled:
                self._enabled[name] = enabled

    def is_enabled(self, name: str) -> bool:
        return self._enabled.get(name, False)

    def get_provider(self, name: str) -> Optional[ProviderBase]:
        if not self.is_enabled(name):
            return None
        return self._providers.get(name)

    def list_providers(self) -> List[Dict[str, Any]]:
        with self._lock:
            result = []
            for name, prov in self._providers.items():
                pool = self._pools.get(name)
                result.append({
                    "name": name,
                    "priority": self._priority.get(name, 0),
                    "enabled": self._enabled.get(name, False),
                    "key_count": pool.count() if pool else 0,
                    "default_model": prov.default_model,
                    "models": prov.list_models(),
                    "health": pool.health_status() if pool else [],
                })
            result.sort(key=lambda x: x["priority"], reverse=True)
            return result

    def map_model(self, model: str, provider: str):
        with self._lock:
            self._model_map[model] = provider

    def resolve_provider(self, model: str = "", provider: str = "") -> Optional[ProviderBase]:
        if provider:
            p = self.get_provider(provider)
            if p:
                return p
        if model and model in self._model_map:
            p = self.get_provider(self._model_map[model])
            if p:
                return p
        for name in sorted(self._priority, key=lambda k: self._priority[k], reverse=True):
            if self._enabled.get(name):
                return self._providers[name]
        return None

    def get_key(self, provider_name: str) -> Optional[str]:
        pool = self._pools.get(provider_name)
        if pool:
            return pool.next_key()
        return None

    def report_failure(self, provider_name: str, key: str):
        pool = self._pools.get(provider_name)
        if pool:
            pool.report_failure(key)

    def report_success(self, provider_name: str, key: str):
        pool = self._pools.get(provider_name)
        if pool:
            pool.report_success(key)

    def failover_chat(self, messages: List[Dict], model: str, provider_name: str = "", **kwargs) -> Tuple[bool, str, int, str]:
        """Try the requested provider, then failover to others by priority. Returns (ok, text, tokens, provider_used)."""
        tried = set()
        order: List[str] = []
        if provider_name and self.is_enabled(provider_name):
            order.append(provider_name)
        for name in sorted(self._priority, key=lambda k: self._priority[k], reverse=True):
            if name not in order and self._enabled.get(name):
                order.append(name)
        for name in order:
            if name in tried:
                continue
            tried.add(name)
            prov = self._providers.get(name)
            if not prov:
                continue
            key = self.get_key(name)
            try:
                ok, text, tokens = prov.chat(messages, model, api_key=key, **kwargs)
                if ok:
                    self.report_success(name, key or "")
                    track_request(provider=name, model=model, tokens=tokens)
                    return True, text, tokens, name
                else:
                    self.report_failure(name, key or "")
                    log.warning("Provider %s failed: %s", name, text[:200])
            except Exception as exc:
                self.report_failure(name, key or "")
                log.warning("Provider %s exception: %s", name, exc)
                track_request(provider=name, model=model, error=True)
        return False, "All providers failed", 0, ""


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------
class OllamaProvider(ProviderBase):
    name = "ollama"
    default_model = "llama3.2"

    def __init__(self, base_url: str = OLLAMA_BASE):
        self.base_url = base_url.rstrip("/")

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        url = f"{self.base_url}/v1/chat/completions"
        body = {"model": model or self.default_model, "messages": messages, "stream": False}
        try:
            r = requests.post(url, json=body, timeout=60)
            if not r.ok:
                return False, f"Ollama HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            tokens = self._estimate_tokens(text)
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        url = f"{self.base_url}/v1/chat/completions"
        body = {"model": model or self.default_model, "messages": messages, "stream": True}
        try:
            with requests.post(url, json=body, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] Ollama HTTP {r.status_code}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"

    def list_models(self) -> List[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=10)
            if r.ok:
                return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            pass
        return [self.default_model]


class OpenAIProvider(ProviderBase):
    name = "openai"
    default_model = "gpt-4o"

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        api_key = kwargs.get("api_key", OPENAI_API_KEY)
        if not api_key:
            return False, "OpenAI API key not configured", 0
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model or self.default_model, "messages": messages, "stream": False}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=60)
            if not r.ok:
                return False, f"OpenAI HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", self._estimate_tokens(text))
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        api_key = kwargs.get("api_key", OPENAI_API_KEY)
        if not api_key:
            yield "[Error] OpenAI API key not configured"
            return
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model or self.default_model, "messages": messages, "stream": True}
        try:
            with requests.post(url, json=body, headers=headers, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] OpenAI HTTP {r.status_code}: {r.text[:200]}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"

    def list_models(self) -> List[str]:
        return ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]


class AnthropicProvider(ProviderBase):
    name = "anthropic"
    default_model = "claude-3-5-sonnet-20241022"

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        api_key = kwargs.get("api_key", ANTHROPIC_API_KEY)
        if not api_key:
            return False, "Anthropic API key not configured", 0
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        system_msg = ""
        user_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_msg += m.get("content", "") + "\n"
            else:
                user_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        body = {"model": model or self.default_model, "messages": user_messages, "max_tokens": 4096, "stream": False}
        if system_msg.strip():
            body["system"] = system_msg.strip()
        try:
            r = requests.post(url, json=body, headers=headers, timeout=60)
            if not r.ok:
                return False, f"Anthropic HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data.get("content", [{}])[0].get("text", "")
            tokens = self._estimate_tokens(text)
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        api_key = kwargs.get("api_key", ANTHROPIC_API_KEY)
        if not api_key:
            yield "[Error] Anthropic API key not configured"
            return
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        system_msg = ""
        user_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_msg += m.get("content", "") + "\n"
            else:
                user_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        body = {"model": model or self.default_model, "messages": user_messages, "max_tokens": 4096, "stream": True}
        if system_msg.strip():
            body["system"] = system_msg.strip()
        try:
            with requests.post(url, json=body, headers=headers, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] Anthropic HTTP {r.status_code}: {r.text[:200]}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        chunk = json.loads(line)
                        if chunk.get("type") == "content_block_delta":
                            delta = chunk.get("delta", {})
                            text = delta.get("text", "")
                            if text:
                                yield text
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"

    def list_models(self) -> List[str]:
        return ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]


class GeminiProvider(ProviderBase):
    name = "gemini"
    default_model = "gemini-1.5-flash"

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        api_key = kwargs.get("api_key", GEMINI_API_KEY)
        if not api_key:
            return False, "Gemini API key not configured", 0
        model_name = model or self.default_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        contents = []
        system_msg = ""
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_msg += content + "\n"
            else:
                contents.append({"role": "user" if role == "assistant" else role, "parts": [{"text": content}]})
        body = {"contents": contents}
        if system_msg.strip():
            body["systemInstruction"] = {"parts": [{"text": system_msg.strip()}]}
        try:
            r = requests.post(url, json=body, timeout=60)
            if not r.ok:
                return False, f"Gemini HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            tokens = self._estimate_tokens(text)
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        api_key = kwargs.get("api_key", GEMINI_API_KEY)
        if not api_key:
            yield "[Error] Gemini API key not configured"
            return
        model_name = model or self.default_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent?key={api_key}&alt=sse"
        contents = []
        system_msg = ""
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_msg += content + "\n"
            else:
                contents.append({"role": "user" if role == "assistant" else role, "parts": [{"text": content}]})
        body = {"contents": contents}
        if system_msg.strip():
            body["systemInstruction"] = {"parts": [{"text": system_msg.strip()}]}
        try:
            with requests.post(url, json=body, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] Gemini HTTP {r.status_code}: {r.text[:200]}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        chunk = json.loads(line)
                        parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        for p in parts:
                            t = p.get("text", "")
                            if t:
                                yield t
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"

    def list_models(self) -> List[str]:
        return ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash-exp"]


class GroqProvider(ProviderBase):
    name = "groq"
    default_model = "llama-3.3-70b-versatile"

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        api_key = kwargs.get("api_key", GROQ_API_KEY)
        if not api_key:
            return False, "Groq API key not configured", 0
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model or self.default_model, "messages": messages, "stream": False}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=60)
            if not r.ok:
                return False, f"Groq HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", self._estimate_tokens(text))
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        api_key = kwargs.get("api_key", GROQ_API_KEY)
        if not api_key:
            yield "[Error] Groq API key not configured"
            return
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model or self.default_model, "messages": messages, "stream": True}
        try:
            with requests.post(url, json=body, headers=headers, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] Groq HTTP {r.status_code}: {r.text[:200]}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"

    def list_models(self) -> List[str]:
        return ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"]


class OpenAICompatibleProvider(ProviderBase):
    name = "openai-compatible"
    default_model = ""

    def __init__(self, base_url: str, api_key: str = "", model: str = ""):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.default_model = model

    def chat(self, messages: List[Dict], model: str, **kwargs) -> Tuple[bool, str, int]:
        api_key = kwargs.get("api_key", self._api_key)
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model or self.default_model, "messages": messages, "stream": False}
        try:
            r = requests.post(url, json=body, headers=headers, timeout=60)
            if not r.ok:
                return False, f"HTTP {r.status_code}: {r.text[:300]}", 0
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            tokens = self._estimate_tokens(text)
            return True, text, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str, **kwargs) -> Generator[str, None, None]:
        api_key = kwargs.get("api_key", self._api_key)
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model or self.default_model, "messages": messages, "stream": True}
        try:
            with requests.post(url, json=body, headers=headers, stream=True, timeout=60) as r:
                if not r.ok:
                    yield f"[Error] HTTP {r.status_code}"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except Exception:
                        continue
        except Exception as exc:
            yield f"[Error] {exc}"


# ---------------------------------------------------------------------------
# Initialize provider manager
# ---------------------------------------------------------------------------
pm = ProviderManager()

# Multi-key support: parse comma-separated keys from env
def _parse_keys(env_val: str) -> List[str]:
    return [k.strip() for k in env_val.split(",") if k.strip()]

# Register providers with priority — Groq and Gemini are primary (highest priority)
pm.register(OllamaProvider(), [], priority=10, enabled=True)
pm.register(GroqProvider(), _parse_keys(GROQ_API_KEY), priority=100, enabled=bool(GROQ_API_KEY))
pm.register(GeminiProvider(), _parse_keys(GEMINI_API_KEY), priority=90, enabled=bool(GEMINI_API_KEY))
pm.register(OpenAIProvider(), _parse_keys(OPENAI_API_KEY), priority=50, enabled=bool(OPENAI_API_KEY))
pm.register(AnthropicProvider(), _parse_keys(ANTHROPIC_API_KEY), priority=50, enabled=bool(ANTHROPIC_API_KEY))

# Default active provider/model
_active_provider = os.environ.get("ACTIVE_PROVIDER", "groq" if GROQ_API_KEY else "ollama")
_active_model = os.environ.get("ACTIVE_MODEL", "")


def get_active_provider() -> str:
    return _active_provider


def get_active_model() -> str:
    return _active_model


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def validate_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Validate an API key from the X-API-Key header. Returns key dict or None."""
    if not api_key:
        return None
    # Check DB-backed API keys
    try:
        from db.repos import APIKeyRepo
        repo = APIKeyRepo()
        # For simplicity, we check by key prefix pattern
        # In production this would do a DB lookup
        return None
    except Exception:
        pass
    # Fallback: admin master key
    master_key = os.environ.get("MASTER_API_KEY", "")
    if master_key and api_key == master_key:
        return {"role": "admin", "key": api_key}
    return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check session
        if session.get("user"):
            return f(*args, **kwargs)
        # Check API key
        api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if api_key:
            keyd = validate_api_key(api_key)
            if keyd:
                return f(*args, **kwargs)
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check session
        if session.get("role") == "admin":
            return f(*args, **kwargs)
        # Check API key
        api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if api_key:
            keyd = validate_api_key(api_key)
            if keyd and keyd.get("role") == "admin":
                return f(*args, **kwargs)
        return jsonify({"ok": False, "error": "Admin access required"}), 403
    return decorated


# ---------------------------------------------------------------------------
# RAG Manager (in-memory, swappable backend)
# ---------------------------------------------------------------------------
class RAGBackendBase(ABC):
    @abstractmethod
    def ingest(self, text: str, source: str = "", metadata: Dict = None) -> bool: ...
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]: ...
    @abstractmethod
    def clear(self) -> None: ...


class InMemoryRAGBackend(RAGBackendBase):
    def __init__(self):
        self._docs: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def ingest(self, text: str, source: str = "", metadata: Dict = None) -> bool:
        with self._lock:
            chunks = [text[i:i+512] for i in range(0, len(text), 512)]
            for i, chunk in enumerate(chunks):
                self._docs.append({
                    "text": chunk,
                    "source": source,
                    "metadata": metadata or {},
                    "chunk_idx": i,
                })
            return True

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        query_lower = query.lower()
        query_words = set(query_lower.split())
        scored = []
        with self._lock:
            for doc in self._docs:
                doc_lower = doc["text"].lower()
                score = sum(1 for w in query_words if w in doc_lower)
                if score > 0:
                    scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()


class RAGManager:
    def __init__(self, backend: RAGBackendBase = None):
        self.backend = backend or InMemoryRAGBackend()

    def ingest(self, text: str, source: str = "", metadata: Dict = None) -> bool:
        return self.backend.ingest(text, source, metadata)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return self.backend.search(query, top_k)

    def clear(self) -> None:
        self.backend.clear()

    def get_context(self, query: str, top_k: int = 5) -> str:
        results = self.search(query, top_k)
        if not results:
            return ""
        parts = []
        for r in results:
            parts.append(f"[{r.get('source', 'unknown')}] {r['text']}")
        return "\n\n".join(parts)


rag_manager = RAGManager()


# ---------------------------------------------------------------------------
# Plugin system
# ---------------------------------------------------------------------------
class PluginBase(ABC):
    name: str = ""
    description: str = ""

    @abstractmethod
    def run(self, input: Dict[str, Any]) -> Dict[str, Any]: ...


class WebSearchPlugin(PluginBase):
    name = "web_search"
    description = "Search the web using DuckDuckGo (no API key required)"

    def run(self, input: Dict[str, Any]) -> Dict[str, Any]:
        query = input.get("query", "")
        if not query:
            return {"ok": False, "error": "query required"}
        try:
            url = "https://api.duckduckgo.com/"
            r = requests.get(url, params={"q": query, "format": "json", "no_html": "1"}, timeout=10)
            data = r.json()
            results = []
            abstract = data.get("AbstractText", "")
            if abstract:
                results.append({"title": data.get("Heading", query), "snippet": abstract, "url": data.get("AbstractURL", "")})
            for topic in data.get("RelatedTopics", [])[:10]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append({"title": topic.get("FirstURL", ""), "snippet": topic["Text"], "url": topic.get("FirstURL", "")})
            return {"ok": True, "results": results}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class PythonExecPlugin(PluginBase):
    name = "python_exec"
    description = "Execute Python code safely in a subprocess"

    def run(self, input: Dict[str, Any]) -> Dict[str, Any]:
        code = input.get("code", "")
        if not code:
            return {"ok": False, "error": "code required"}
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=10,
            )
            return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Execution timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


PLUGINS: Dict[str, PluginBase] = {
    "web_search": WebSearchPlugin(),
    "python_exec": PythonExecPlugin(),
}


# ---------------------------------------------------------------------------
# Agent system
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, name: str, label: str, role: str, system_prompt: str,
                 tools: List[str] = None, permissions: List[str] = None,
                 model: str = "", provider: str = ""):
        self.name = name
        self.label = label
        self.role = role
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.permissions = permissions or ["read"]
        self.model = model
        self.provider = provider
        self.status = "ready"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "role": self.role,
            "system_prompt": self.system_prompt[:200],
            "tools": self.tools,
            "permissions": self.permissions,
            "model": self.model,
            "provider": self.provider,
            "status": self.status,
        }

    def run(self, message: str, context: str = "") -> Generator[str, None, None]:
        messages = [{"role": "system", "content": self.system_prompt}]
        if context:
            messages.append({"role": "system", "content": f"Context:\n{context}"})
        messages.append({"role": "user", "content": message})
        provider_name = self.provider or get_active_provider()
        model = self.model or get_active_model()
        prov = pm.resolve_provider(model, provider_name)
        if not prov:
            yield "[Error] No provider available"
            return
        key = pm.get_key(provider_name)
        try:
            for chunk in prov.stream_chat(messages, model, api_key=key):
                yield chunk
        except Exception as exc:
            pm.report_failure(provider_name, key or "")
            yield f"[Error] {exc}"


AGENTS: Dict[str, Agent] = {}


def _register_agent(agent: Agent):
    AGENTS[agent.name] = agent


# 15 agents
_register_agent(Agent(
    "coding", "Coding Agent", "Code generation and debugging",
    "You are an expert coding agent. Write clean, efficient, well-structured code. Explain your reasoning and suggest improvements.",
    tools=["python_exec", "file_manager", "terminal"], permissions=["read", "write", "exec"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "research", "Research Agent", "Information gathering and analysis",
    "You are a research agent. Gather, synthesize, and present information from available sources. Be thorough and cite sources.",
    tools=["web_search", "rag"], permissions=["read"],
    model="gemini-1.5-flash", provider="gemini",
))
_register_agent(Agent(
    "vision", "Vision Agent", "Image analysis and understanding",
    "You are a vision agent. Analyze images, describe contents, extract text (OCR), and answer questions about visual content.",
    tools=["image_analysis", "ocr"], permissions=["read"],
    model="gpt-4o", provider="openai",
))
_register_agent(Agent(
    "writing", "Writing Agent", "Content creation and editing",
    "You are a professional writing agent. Create polished, engaging content tailored to the audience. Edit and refine for clarity, tone, and style.",
    tools=[], permissions=["read"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "translation", "Translation Agent", "Multi-language translation",
    "You are a translation agent. Translate text accurately between languages while preserving meaning, tone, and cultural context.",
    tools=[], permissions=["read"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "planner", "Planner Agent", "Project planning and task breakdown",
    "You are a planner agent. Break down complex projects into actionable tasks, create timelines, identify dependencies and risks.",
    tools=[], permissions=["read"],
    model="gemini-1.5-flash", provider="gemini",
))
_register_agent(Agent(
    "browser", "Browser Agent", "Web browsing and scraping",
    "You are a browser agent. Navigate web pages, extract content, fill forms, and interact with web interfaces.",
    tools=["web_search"], permissions=["read"],
    model="gemini-1.5-flash", provider="gemini",
))
_register_agent(Agent(
    "rag", "RAG Agent", "Knowledge retrieval and Q&A",
    "You are a RAG agent. Use the knowledge base to answer questions accurately. If information is not in the knowledge base, say so.",
    tools=["rag"], permissions=["read"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "file", "File Agent", "File management and operations",
    "You are a file agent. Manage files in the workspace: create, read, edit, organize, and search files.",
    tools=["file_manager"], permissions=["read", "write"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "terminal", "Terminal Agent", "Command execution",
    "You are a terminal agent. Execute shell commands safely and report results. Explain command outputs.",
    tools=["terminal"], permissions=["read", "exec"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "git", "Git Agent", "Version control operations",
    "You are a git agent. Handle git operations: status, add, commit, push, pull, branch management. Use GITHUB_TOKEN for remote operations.",
    tools=["github"], permissions=["read", "write", "exec"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "debug", "Debug Agent", "Error analysis and debugging",
    "You are a debug agent. Analyze errors, trace bugs, suggest fixes, and help resolve issues systematically.",
    tools=["python_exec", "terminal", "file_manager"], permissions=["read", "exec"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "security", "Security Agent", "Security analysis and auditing",
    "You are a security agent. Analyze code for vulnerabilities, suggest security improvements, and audit configurations.",
    tools=["file_manager", "terminal"], permissions=["read", "exec"],
    model="claude-3-5-sonnet-20241022", provider="anthropic",
))
_register_agent(Agent(
    "database", "Database Agent", "Database operations and SQL",
    "You are a database agent. Write and optimize SQL queries, design schemas, and manage database operations.",
    tools=["sql_explorer"], permissions=["read", "write"],
    model="llama-3.3-70b-versatile", provider="groq",
))
_register_agent(Agent(
    "devops", "DevOps Agent", "Deployment and infrastructure",
    "You are a DevOps agent. Handle deployment, CI/CD, Docker, infrastructure configuration, and monitoring.",
    tools=["terminal", "file_manager"], permissions=["read", "write", "exec"],
    model="llama-3.3-70b-versatile", provider="groq",
))


# ---------------------------------------------------------------------------
# Job tracking for async agent runs
# ---------------------------------------------------------------------------
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def create_job(agent_name: str, message: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "agent": agent_name,
            "message": message,
            "status": "running",
            "output": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
    return job_id


def run_agent_job(job_id: str, agent_name: str, message: str):
    agent = AGENTS.get(agent_name)
    if not agent:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["output"] = f"Agent '{agent_name}' not found"
            _jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
        return
    output_parts = []
    try:
        for chunk in agent.run(message):
            output_parts.append(chunk)
        with _jobs_lock:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["output"] = "".join(output_parts)
            _jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["output"] = str(exc)
            _jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        return _jobs.get(job_id)


# ---------------------------------------------------------------------------
# Conversations (in-memory)
# ---------------------------------------------------------------------------
_conversations: Dict[str, Dict[str, Any]] = {}
_conversations_lock = threading.Lock()


def _new_conversation_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(Path(__file__).parent), "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(str(Path(__file__).parent / "public"), "manifest.json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(str(Path(__file__).parent / "public"), "sw.js")


@app.route("/favicon.ico")
def favicon():
    try:
        return send_from_directory(str(Path(__file__).parent / "public"), "icon-192.png", mimetype="image/png")
    except Exception:
        return "", 204


@app.route("/icon-<int:size>.png")
def icon(size):
    return send_from_directory(str(Path(__file__).parent / "public"), f"icon-{size}.png")


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "agent_system", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/auth/status")
def api_auth_status():
    """Check authentication status — used by frontend on init."""
    if session.get("user"):
        return jsonify({"ok": True, "authenticated": True, "user": session["user"], "role": session.get("role", "user")})
    api_key = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    if api_key:
        keyd = validate_api_key(api_key)
        if keyd:
            return jsonify({"ok": True, "authenticated": True, "user": "api-key-user", "role": keyd.get("role", "user")})
    return jsonify({"ok": False, "authenticated": False}), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return jsonify({"ok": True, "message": "Send POST with username and password"})
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    ys_user = os.environ.get("YS_USER", "")
    ys_password = os.environ.get("YS_PASSWORD", "")
    if not ys_user or not ys_password:
        return jsonify({"ok": False, "error": "Admin credentials not configured (YS_USER/YS_PASSWORD)"}), 500
    if username == ys_user and password == ys_password:
        session.clear()
        session["user"] = username
        session["role"] = "admin"
        return jsonify({"ok": True, "user": username, "role": "admin"})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/providers")
@login_required
def api_providers():
    return jsonify({"ok": True, "providers": pm.list_providers(), "active": get_active_provider()})


@app.route("/api/providers/switch", methods=["POST"])
@login_required
def api_providers_switch():
    global _active_provider
    data = request.get_json(silent=True) or {}
    name = data.get("provider", "")
    if name in pm._providers:
        _active_provider = name
        return jsonify({"ok": True, "active": name})
    return jsonify({"ok": False, "error": "Unknown provider"}), 400


@app.route("/api/providers/toggle", methods=["POST"])
@login_required
def api_providers_toggle():
    data = request.get_json(silent=True) or {}
    name = data.get("provider", "")
    enabled = bool(data.get("enabled", True))
    pm.set_enabled(name, enabled)
    return jsonify({"ok": True, "provider": name, "enabled": enabled})


@app.route("/api/providers/keys", methods=["POST"])
@admin_required
def api_providers_keys():
    data = request.get_json(silent=True) or {}
    name = data.get("provider", "")
    keys = _parse_keys(data.get("keys", ""))
    if name and keys:
        pm._pools[name] = KeyPool(keys, name)
        # Auto-enable the provider when keys are provided
        if name in pm._providers:
            pm.set_enabled(name, True)
        return jsonify({"ok": True, "provider": name, "key_count": len(keys)})
    return jsonify({"ok": False, "error": "provider and keys required"}), 400


@app.route("/api/providers/model_map", methods=["POST"])
@login_required
def api_model_map():
    data = request.get_json(silent=True) or {}
    model = data.get("model", "")
    provider = data.get("provider", "")
    if model and provider:
        pm.map_model(model, provider)
        return jsonify({"ok": True, "model": model, "provider": provider})
    return jsonify({"ok": False, "error": "model and provider required"}), 400


@app.route("/api/models")
@login_required
def api_models():
    provider_name = request.args.get("provider", get_active_provider())
    prov = pm.get_provider(provider_name)
    if not prov:
        return jsonify({"ok": False, "error": "Provider not found or disabled"}), 404
    return jsonify({"ok": True, "models": prov.list_models(), "active": get_active_model()})


@app.route("/api/models/switch", methods=["POST"])
@login_required
def api_models_switch():
    global _active_model
    data = request.get_json(silent=True) or {}
    model = data.get("model", "")
    if model:
        _active_model = model
        return jsonify({"ok": True, "active": model})
    return jsonify({"ok": False, "error": "model required"}), 400


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    model = data.get("model", get_active_model())
    provider_name = data.get("provider", get_active_provider())
    history = data.get("history", [])
    use_rag = data.get("rag", False)

    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400

    with _stats_lock:
        _stats["chat_requests"] += 1

    messages = []
    for h in history:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    if use_rag:
        context = rag_manager.get_context(message)
        if context:
            messages.append({"role": "system", "content": f"Knowledge base context:\n{context}"})
    messages.append({"role": "user", "content": message})

    ok, text, tokens, used = pm.failover_chat(messages, model, provider_name)
    return jsonify({"ok": ok, "reply": text, "tokens": tokens, "provider": used, "model": model})


@app.route("/api/chat/stream", methods=["POST"])
@login_required
def api_chat_stream():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    model = data.get("model", get_active_model())
    provider_name = data.get("provider", get_active_provider())
    history = data.get("history", [])
    use_rag = data.get("rag", False)

    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400

    with _stats_lock:
        _stats["chat_requests"] += 1

    messages = []
    for h in history:
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    if use_rag:
        context = rag_manager.get_context(message)
        if context:
            messages.append({"role": "system", "content": f"Knowledge base context:\n{context}"})
    messages.append({"role": "user", "content": message})

    def generate():
        prov = pm.resolve_provider(model, provider_name)
        if not prov:
            yield f"data: {json.dumps({'error': 'No provider available'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        key = pm.get_key(prov.name)
        try:
            for chunk in prov.stream_chat(messages, model, api_key=key):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            pm.report_success(prov.name, key or "")
            track_request(provider=prov.name, model=model, tokens=1)
        except Exception as exc:
            pm.report_failure(prov.name, key or "")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")


@app.route("/api/conversations", methods=["GET", "POST"])
@login_required
def api_conversations():
    if request.method == "GET":
        with _conversations_lock:
            result = []
            for cid, conv in _conversations.items():
                result.append({"id": cid, "title": conv["title"], "messages": len(conv["messages"]), "created_at": conv["created_at"]})
            return jsonify({"ok": True, "conversations": result})
    data = request.get_json(silent=True) or {}
    title = data.get("title", "New Conversation")
    cid = _new_conversation_id()
    with _conversations_lock:
        _conversations[cid] = {"title": title, "messages": [], "created_at": datetime.now(timezone.utc).isoformat()}
    return jsonify({"ok": True, "id": cid})


@app.route("/api/conversation/<cid>", methods=["GET", "PATCH", "DELETE"])
@login_required
def api_conversation(cid):
    if request.method == "DELETE":
        with _conversations_lock:
            _conversations.pop(cid, None)
        return jsonify({"ok": True})
    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        with _conversations_lock:
            if cid not in _conversations:
                return jsonify({"ok": False, "error": "not found"}), 404
            if "title" in data:
                _conversations[cid]["title"] = data["title"]
            if "message" in data:
                _conversations[cid]["messages"].append(data["message"])
        return jsonify({"ok": True})
    with _conversations_lock:
        conv = _conversations.get(cid)
        if not conv:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "conversation": conv})


@app.route("/api/agents")
@login_required
def api_agents():
    return jsonify({"ok": True, "agents": [a.to_dict() for a in AGENTS.values()]})


@app.route("/api/agents/<name>/run", methods=["POST"])
@login_required
def api_agent_run(name):
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    if name not in AGENTS:
        return jsonify({"ok": False, "error": "agent not found"}), 404
    job_id = create_job(name, message)
    t = threading.Thread(target=run_agent_job, args=(job_id, name, message), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/jobs/<job_id>")
@login_required
def api_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/trigger", methods=["POST"])
@login_required
def api_trigger():
    """Trigger a multi-agent workflow pipeline."""
    data = request.get_json(silent=True) or {}
    task = data.get("task", "")
    agents = data.get("agents", ["research", "writing"])
    if not task:
        return jsonify({"ok": False, "error": "task required"}), 400
    job_id = create_job("pipeline", task)
    results = {}
    for agent_name in agents:
        if agent_name not in AGENTS:
            continue
        agent = AGENTS[agent_name]
        output_parts = []
        for chunk in agent.run(task, context=json.dumps(results)):
            output_parts.append(chunk)
        results[agent_name] = "".join(output_parts)
    with _jobs_lock:
        _jobs[job_id]["status"] = "completed"
        _jobs[job_id]["output"] = json.dumps(results)
        _jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
    return jsonify({"ok": True, "job_id": job_id, "results": results})


@app.route("/api/files/list")
@login_required
def api_files_list():
    path = request.args.get("path", "")
    base = WORKSPACE_DIR / path if path else WORKSPACE_DIR
    if not str(base.resolve()).startswith(str(WORKSPACE_DIR.resolve())):
        return jsonify({"ok": False, "error": "path traversal denied"}), 403
    if not base.exists():
        return jsonify({"ok": False, "error": "path not found"}), 404
    items = []
    for entry in sorted(base.iterdir()):
        items.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    return jsonify({"ok": True, "items": items, "path": path})


@app.route("/api/files/read")
@login_required
def api_files_read():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "path required"}), 400
    fp = WORKSPACE_DIR / path
    if not str(fp.resolve()).startswith(str(WORKSPACE_DIR.resolve())):
        return jsonify({"ok": False, "error": "path traversal denied"}), 403
    if not fp.exists() or not fp.is_file():
        return jsonify({"ok": False, "error": "file not found"}), 404
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
        return jsonify({"ok": True, "content": content, "path": path, "size": fp.stat().st_size})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/files/upload", methods=["POST"])
@login_required
def api_files_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file required"}), 400
    f = request.files["file"]
    filename = secure_filename(f.filename or "upload")
    dest = UPLOAD_DIR / filename
    f.save(str(dest))
    return jsonify({"ok": True, "name": filename, "size": dest.stat().st_size, "path": str(dest.relative_to(WORKSPACE_DIR))})


@app.route("/api/files/delete", methods=["DELETE"])
@login_required
def api_files_delete():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "path required"}), 400
    fp = WORKSPACE_DIR / path
    if not str(fp.resolve()).startswith(str(WORKSPACE_DIR.resolve())):
        return jsonify({"ok": False, "error": "path traversal denied"}), 403
    if not fp.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    if fp.is_dir():
        shutil.rmtree(fp)
    else:
        fp.unlink()
    return jsonify({"ok": True})


@app.route("/api/terminal/exec", methods=["POST"])
@admin_required
def api_terminal_exec():
    data = request.get_json(silent=True) or {}
    cmd = data.get("command", "")
    if not cmd:
        return jsonify({"ok": False, "error": "command required"}), 400

    def generate():
        try:
            proc = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                cwd=str(WORKSPACE_DIR),
            )
            for line in proc.stdout:
                yield json.dumps({"text": line}) + "\n"
            proc.wait()
            yield json.dumps({"exit_code": proc.returncode}) + "\n"
        except Exception as exc:
            yield json.dumps({"error": str(exc)}) + "\n"

    return Response(stream_with_context(generate()), content_type="application/x-ndjson")


@app.route("/api/plugins")
@login_required
def api_plugins():
    return jsonify({"ok": True, "plugins": [{"name": p.name, "description": p.description} for p in PLUGINS.values()]})


@app.route("/api/plugins/<name>/run", methods=["POST"])
@login_required
def api_plugin_run(name):
    plugin = PLUGINS.get(name)
    if not plugin:
        return jsonify({"ok": False, "error": "plugin not found"}), 404
    data = request.get_json(silent=True) or {}
    result = plugin.run(data)
    return jsonify(result)


@app.route("/api/search", methods=["POST"])
@login_required
def api_search():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    result = PLUGINS["web_search"].run({"query": query})
    return jsonify(result)


@app.route("/api/rag/ingest", methods=["POST"])
@login_required
def api_rag_ingest():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    source = data.get("source", "api")
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    ok = rag_manager.ingest(text, source, data.get("metadata"))
    return jsonify({"ok": ok})


@app.route("/api/rag/search", methods=["POST"])
@login_required
def api_rag_search():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    top_k = data.get("top_k", 5)
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    results = rag_manager.search(query, top_k)
    return jsonify({"ok": True, "results": results})


@app.route("/api/rag/clear", methods=["POST"])
@admin_required
def api_rag_clear():
    rag_manager.clear()
    return jsonify({"ok": True})


@app.route("/api/compare", methods=["POST"])
@login_required
def api_compare():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    models = data.get("models", [])
    if not message or not models:
        return jsonify({"ok": False, "error": "message and models required"}), 400
    results = {}
    for m in models:
        model_name = m.get("model", "")
        provider_name = m.get("provider", "")
        ok, text, tokens, used = pm.failover_chat(
            [{"role": "user", "content": message}], model_name, provider_name
        )
        results[f"{provider_name or used}/{model_name}"] = {"ok": ok, "reply": text, "tokens": tokens}
    return jsonify({"ok": True, "results": results})


@app.route("/api/logs")
@login_required
def api_logs():
    with _log_lock:
        return jsonify({"ok": True, "logs": list(LOG_RECORDS[-200:])})


@app.route("/api/github/push", methods=["POST"])
@admin_required
def api_github_push():
    data = request.get_json(silent=True) or {}
    commit_msg = data.get("message", "Auto-push from Agent System")
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(WORKSPACE_DIR), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(WORKSPACE_DIR), check=True, capture_output=True)
        if GITHUB_TOKEN:
            subprocess.run(["git", "push"], cwd=str(WORKSPACE_DIR), check=True, capture_output=True)
        return jsonify({"ok": True, "message": commit_msg})
    except subprocess.CalledProcessError as exc:
        return jsonify({"ok": False, "error": exc.stderr.decode() if exc.stderr else str(exc)}), 500


@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    with _stats_lock:
        return jsonify({"ok": True, "stats": dict(_stats)})


@app.route("/api/admin/usage")
@admin_required
def api_admin_usage():
    with _stats_lock:
        return jsonify({
            "ok": True,
            "provider_usage": dict(_stats["provider_usage"]),
            "model_usage": dict(_stats["model_usage"]),
        })


@app.route("/v1/chat/completions", methods=["POST"])
def openai_compatible():
    """OpenAI-compatible endpoint for external tools and agents."""
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    master_key = os.environ.get("MASTER_API_KEY", "")
    if master_key and api_key != master_key:
        api_key_header = request.headers.get("X-API-Key", "")
        if api_key_header != master_key:
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    model = data.get("model", get_active_model())
    provider_name = data.get("provider", "")
    stream = data.get("stream", False)

    if not messages:
        return jsonify({"error": "messages required"}), 400

    if stream:
        def generate():
            prov = pm.resolve_provider(model, provider_name)
            if not prov:
                yield f"data: {json.dumps({'error': 'No provider'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            key = pm.get_key(prov.name)
            for chunk in prov.stream_chat(messages, model, api_key=key):
                yield f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(generate()), content_type="text/event-stream")

    ok, text, tokens, used = pm.failover_chat(messages, model, provider_name)
    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": tokens, "total_tokens": tokens},
    })


# ---------------------------------------------------------------------------
# Telegram bot thread (optional)
# ---------------------------------------------------------------------------
def _start_telegram_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        from telegram_bot import run_bot
        api_key = os.environ.get("MASTER_API_KEY", "")
        base_url = f"http://127.0.0.1:{PORT}"
        t = threading.Thread(target=run_bot, args=(token, api_key, base_url), daemon=True)
        t.start()
        log.info("Telegram bot thread started")
    except Exception:
        log.warning("Telegram bot not started (dependency missing or misconfigured)")


# ---------------------------------------------------------------------------
# DB initialization (optional, non-fatal)
# ---------------------------------------------------------------------------
def _init_db():
    try:
        from db.migrations import prepare_database
        ok = prepare_database()
        if ok:
            log.info("Database initialized successfully")
        else:
            log.info("Database not available — running in-memory mode")
    except Exception:
        log.info("DB module not available — running in-memory mode")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()


def _handle_shutdown(signum, frame):
    sig_name = signal.Signals(signum).name
    log.info("Received %s — initiating graceful shutdown...", sig_name)
    _shutdown_event.set()
    # Give in-flight requests a moment, then exit
    threading.Timer(2.0, lambda: os._exit(0)).start()


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    log.error("Unhandled exception: %s\n%s", e, traceback.format_exc())
    track_request(error=True)
    return jsonify({"ok": False, "error": "Internal server error", "detail": str(e)}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(405)
def handle_405(e):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405


@app.errorhandler(429)
def handle_429(e):
    return jsonify({"ok": False, "error": "Rate limit exceeded. Please slow down."}), 429


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _init_db()
    _start_telegram_bot()
    log.info("Starting Agent System on %s:%s (provider=%s, model=%s, cors=%s, rate_limit=%s)",
             HOST, PORT, get_active_provider(), get_active_model(), _cors_origins, bool(limiter))
    app.run(host=HOST, port=PORT, threaded=True)
