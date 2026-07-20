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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
YS_USER = os.environ.get("YS_USER", "")
YS_PASSWORD = os.environ.get("YS_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
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


class OllamaProvider(ProviderBase):
    name = "ollama"
    default_model = DEFAULT_MODEL

    def __init__(self, base_url: str = OLLAMA_BASE) -> None:
        self.base_url = base_url.rstrip("/")
        self.completions_url = f"{self.base_url}/v1/chat/completions"

    def list_models(self) -> List[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if r.ok:
                data = r.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                return [m for m in models if m]
        except Exception:
            pass
        # fallback: try /v1/models
        try:
            r = requests.get(f"{self.base_url}/v1/models", timeout=5)
            if r.ok:
                data = r.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                return [m for m in models if m]
        except Exception:
            pass
        return [self.default_model]

    def chat(self, messages: List[Dict], model: str = DEFAULT_MODEL, **kwargs) -> Tuple[bool, str, int]:
        payload = {"model": model or self.default_model, "messages": messages, "stream": False}
        try:
            r = requests.post(self.completions_url, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                tokens = usage.get("total_tokens", self._estimate_tokens(content))
                return True, content, tokens
            return False, "No choices in response", 0
        except requests.exceptions.ConnectionError:
            return False, f"Cannot connect to Ollama at {self.base_url}. Ensure Ollama is running.", 0
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str = DEFAULT_MODEL, **kwargs) -> Generator[str, None, None]:
        payload = {"model": model or self.default_model, "messages": messages, "stream": True}
        try:
            with requests.post(self.completions_url, json=payload, timeout=120, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    if decoded.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(decoded)
                        delta = obj.get("choices", [{}])[0].get("delta", {})
                        chunk = delta.get("content", "")
                        if chunk:
                            yield chunk
                    except json.JSONDecodeError:
                        continue
        except requests.exceptions.ConnectionError:
            yield f"\n[Error] Cannot connect to Ollama at {self.base_url}. Ensure Ollama is running."
        except Exception as exc:
            yield f"\n[Error] {exc}"


class OpenAIProvider(ProviderBase):
    name = "openai"
    default_model = "gpt-4o-mini"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or OPENAI_API_KEY
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def _headers(self) -> Dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def list_models(self) -> List[str]:
        return ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]

    def chat(self, messages: List[Dict], model: str = "", **kwargs) -> Tuple[bool, str, int]:
        if not self.api_key:
            return False, "OPENAI_API_KEY not configured", 0
        model = model or self.default_model
        payload = {"model": model, "messages": messages, "stream": False}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=120)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", self._estimate_tokens(content))
            return True, content, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str = "", **kwargs) -> Generator[str, None, None]:
        if not self.api_key:
            yield "[Error] OPENAI_API_KEY not configured"
            return
        model = model or self.default_model
        payload = {"model": model, "messages": messages, "stream": True}
        try:
            with requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=120, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    if decoded.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(decoded)
                        chunk = obj["choices"][0]["delta"].get("content", "")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue
        except Exception as exc:
            yield f"\n[Error] {exc}"


class AnthropicProvider(ProviderBase):
    name = "anthropic"
    default_model = "claude-3-5-sonnet-20241022"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.base_url = "https://api.anthropic.com/v1"

    def _headers(self) -> Dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def list_models(self) -> List[str]:
        return ["claude-3-5-sonnet-20241022", "claude-3-haiku-20240307", "claude-3-opus-20240229"]

    def chat(self, messages: List[Dict], model: str = "", **kwargs) -> Tuple[bool, str, int]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not configured", 0
        model = model or self.default_model
        system_msgs = [m["content"] for m in messages if m.get("role") == "system"]
        user_msgs = [m for m in messages if m.get("role") != "system"]
        payload: Dict[str, Any] = {"model": model, "max_tokens": 4096, "messages": user_msgs}
        if system_msgs:
            payload["system"] = "\n".join(system_msgs)
        try:
            r = requests.post(f"{self.base_url}/messages", json=payload, headers=self._headers(), timeout=120)
            r.raise_for_status()
            data = r.json()
            content = data["content"][0]["text"]
            tokens = data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0)
            return True, content, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str = "", **kwargs) -> Generator[str, None, None]:
        if not self.api_key:
            yield "[Error] ANTHROPIC_API_KEY not configured"
            return
        model = model or self.default_model
        system_msgs = [m["content"] for m in messages if m.get("role") == "system"]
        user_msgs = [m for m in messages if m.get("role") != "system"]
        payload: Dict[str, Any] = {"model": model, "max_tokens": 4096, "messages": user_msgs, "stream": True}
        if system_msgs:
            payload["system"] = "\n".join(system_msgs)
        try:
            with requests.post(f"{self.base_url}/messages", json=payload, headers=self._headers(), timeout=120, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    try:
                        obj = json.loads(decoded)
                        delta = obj.get("delta", {})
                        chunk = delta.get("text", "")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue
        except Exception as exc:
            yield f"\n[Error] {exc}"


class GeminiProvider(ProviderBase):
    name = "gemini"
    default_model = "gemini-1.5-flash"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or GEMINI_API_KEY
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    def list_models(self) -> List[str]:
        return ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"]

    def _to_gemini_messages(self, messages: List[Dict]) -> List[Dict]:
        result = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                result.append({"role": "user", "parts": [{"text": f"[SYSTEM] {m['content']}"}]})
                result.append({"role": "model", "parts": [{"text": "Understood."}]})
            elif role == "assistant":
                result.append({"role": "model", "parts": [{"text": m["content"]}]})
            else:
                result.append({"role": "user", "parts": [{"text": m["content"]}]})
        return result

    def chat(self, messages: List[Dict], model: str = "", **kwargs) -> Tuple[bool, str, int]:
        if not self.api_key:
            return False, "GEMINI_API_KEY not configured", 0
        model = model or self.default_model
        url = f"{self.base_url}/models/{model}:generateContent?key={self.api_key}"
        payload = {"contents": self._to_gemini_messages(messages)}
        try:
            r = requests.post(url, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return True, text, self._estimate_tokens(text)
        except Exception as exc:
            return False, str(exc), 0


class GroqProvider(ProviderBase):
    name = "groq"
    default_model = "llama-3.1-70b-versatile"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key or GROQ_API_KEY
        self.base_url = "https://api.groq.com/openai/v1"

    def _headers(self) -> Dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def list_models(self) -> List[str]:
        return ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma-7b-it"]

    def chat(self, messages: List[Dict], model: str = "", **kwargs) -> Tuple[bool, str, int]:
        if not self.api_key:
            return False, "GROQ_API_KEY not configured", 0
        model = model or self.default_model
        payload = {"model": model, "messages": messages}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=60)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", self._estimate_tokens(content))
            return True, content, tokens
        except Exception as exc:
            return False, str(exc), 0

    def stream_chat(self, messages: List[Dict], model: str = "", **kwargs) -> Generator[str, None, None]:
        if not self.api_key:
            yield "[Error] GROQ_API_KEY not configured"
            return
        model = model or self.default_model
        payload = {"model": model, "messages": messages, "stream": True}
        try:
            with requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=60, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    if decoded.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(decoded)
                        chunk = obj["choices"][0]["delta"].get("content", "")
                        if chunk:
                            yield chunk
                    except Exception:
                        continue
        except Exception as exc:
            yield f"\n[Error] {exc}"


class OpenAICompatibleProvider(ProviderBase):
    """Generic OpenAI-compatible provider for custom endpoints."""
    name = "openai_compatible"
    default_model = "default"

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "default") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = model

    def _headers(self) -> Dict:
        h: Dict = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, messages: List[Dict], model: str = "", **kwargs) -> Tuple[bool, str, int]:
        model = model or self.default_model
        payload = {"model": model, "messages": messages}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=120)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", self._estimate_tokens(content))
            return True, content, tokens
        except Exception as exc:
            return False, str(exc), 0


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------
class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, ProviderBase] = {}
        self._active_name: str = DEFAULT_PROVIDER
        self._active_model: str = DEFAULT_MODEL
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._providers["ollama"] = OllamaProvider()
        self._providers["openai"] = OpenAIProvider()
        self._providers["anthropic"] = AnthropicProvider()
        self._providers["gemini"] = GeminiProvider()
        self._providers["groq"] = GroqProvider()

    def register(self, provider: ProviderBase) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str = "") -> ProviderBase:
        return self._providers.get(name or self._active_name, self._providers["ollama"])

    def list_providers(self) -> List[Dict]:
        result = []
        for name, p in self._providers.items():
            result.append({
                "name": name,
                "active": name == self._active_name,
                "default_model": p.default_model,
            })
        return result

    def set_active(self, provider_name: str, model: str = "") -> bool:
        if provider_name not in self._providers:
            return False
        self._active_name = provider_name
        if model:
            self._active_model = model
        else:
            self._active_model = self._providers[provider_name].default_model
        return True

    @property
    def active_provider(self) -> ProviderBase:
        return self.get(self._active_name)

    @property
    def active_model(self) -> str:
        return self._active_model

    @active_model.setter
    def active_model(self, value: str) -> None:
        self._active_model = value

    def list_models(self, provider_name: str = "") -> List[str]:
        return self.get(provider_name or self._active_name).list_models()


REGISTRY = ProviderRegistry()

# ---------------------------------------------------------------------------
# RAG Architecture (interfaces — requires external vector store installation)
# ---------------------------------------------------------------------------
class RAGBackendBase(ABC):
    """Abstract RAG backend. Implement with ChromaDB, Qdrant, PGVector, or Milvus."""

    @abstractmethod
    def add_documents(self, documents: List[Dict]) -> None:
        """Ingest document chunks: [{id, text, metadata}]."""

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """Return [{id, text, score, metadata}]."""

    @abstractmethod
    def delete(self, doc_id: str) -> None:
        """Delete document by id."""


class InMemoryRAGBackend(RAGBackendBase):
    """Simple in-memory vector search using TF-IDF approximation."""

    def __init__(self) -> None:
        self._docs: List[Dict] = []

    def add_documents(self, documents: List[Dict]) -> None:
        self._docs.extend(documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        query_words = set(query.lower().split())
        scored = []
        for doc in self._docs:
            text_words = set(doc.get("text", "").lower().split())
            overlap = len(query_words & text_words)
            if overlap > 0:
                scored.append({**doc, "score": overlap / max(len(query_words), 1)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def delete(self, doc_id: str) -> None:
        self._docs = [d for d in self._docs if d.get("id") != doc_id]


class RAGManager:
    """Manages document ingestion and retrieval across backends."""

    def __init__(self) -> None:
        self._backend: RAGBackendBase = InMemoryRAGBackend()
        self._backend_name: str = "memory"

    def set_backend(self, backend: RAGBackendBase, name: str = "") -> None:
        self._backend = backend
        self._backend_name = name

    def ingest_text(self, text: str, metadata: Dict = None, chunk_size: int = 500) -> int:
        words = text.split()
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunk_text = " ".join(words[i:i + chunk_size])
            chunks.append({
                "id": uuid.uuid4().hex,
                "text": chunk_text,
                "metadata": metadata or {},
            })
        self._backend.add_documents(chunks)
        return len(chunks)

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        return self._backend.search(query, top_k)

    def build_context(self, query: str, top_k: int = 5) -> str:
        results = self.search(query, top_k)
        if not results:
            return ""
        parts = [f"[Source {i+1}] {r['text']}" for i, r in enumerate(results)]
        return "\n\n".join(parts)

    @property
    def backend_name(self) -> str:
        return self._backend_name


RAG = RAGManager()

# ---------------------------------------------------------------------------
# Conversation store
# ---------------------------------------------------------------------------
_conv_lock = threading.Lock()
_conversations: Dict[str, Dict] = {}      # id -> conversation
_user_convs: Dict[str, List[str]] = {}    # user_id -> [conv_id]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_conversation(user_id: str, title: str = "New Chat", folder: str = "", tags: List[str] = None) -> Dict:
    conv_id = uuid.uuid4().hex
    conv = {
        "id": conv_id,
        "title": title,
        "folder": folder,
        "tags": tags or [],
        "favorite": False,
        "messages": [],
        "created": time.time(),
        "updated": time.time(),
        "provider": REGISTRY._active_name,
        "model": REGISTRY.active_model,
    }
    with _conv_lock:
        _conversations[conv_id] = conv
        _user_convs.setdefault(user_id, []).append(conv_id)
    return conv


def get_conversations(user_id: str) -> List[Dict]:
    with _conv_lock:
        ids = _user_convs.get(user_id, [])
        convs = [_conversations[i] for i in ids if i in _conversations]
    convs.sort(key=lambda c: c["updated"], reverse=True)
    return convs


def get_conversation(conv_id: str) -> Optional[Dict]:
    with _conv_lock:
        return _conversations.get(conv_id)


def update_conversation(conv_id: str, **fields) -> Optional[Dict]:
    with _conv_lock:
        conv = _conversations.get(conv_id)
        if conv is None:
            return None
        for k, v in fields.items():
            conv[k] = v
        conv["updated"] = time.time()
        return dict(conv)


def delete_conversation(conv_id: str, user_id: str) -> bool:
    with _conv_lock:
        if conv_id not in _conversations:
            return False
        del _conversations[conv_id]
        if user_id in _user_convs:
            _user_convs[user_id] = [i for i in _user_convs[user_id] if i != conv_id]
        return True


def append_message(conv_id: str, role: str, content: str, model: str = "", provider: str = "", tokens: int = 0) -> Dict:
    msg = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "time": _now_iso(),
        "model": model,
        "provider": provider,
        "tokens": tokens,
    }
    with _conv_lock:
        conv = _conversations.get(conv_id)
        if conv:
            conv["messages"].append(msg)
            conv["updated"] = time.time()
            if role == "user" and conv["title"] == "New Chat" and content:
                conv["title"] = content[:60].replace("\n", " ")
    return msg


# ---------------------------------------------------------------------------
# File store
# ---------------------------------------------------------------------------
_files_lock = threading.Lock()
_file_meta: Dict[str, Dict] = {}   # relative_path -> metadata


def _safe_path(path: str) -> Path:
    """Ensure path is inside WORKSPACE_DIR."""
    resolved = (WORKSPACE_DIR / path).resolve()
    if not str(resolved).startswith(str(WORKSPACE_DIR.resolve())):
        raise ValueError("Path traversal detected")
    return resolved


def list_files(subpath: str = "") -> List[Dict]:
    try:
        target = _safe_path(subpath)
        if not target.exists():
            target = WORKSPACE_DIR
        entries = []
        for p in sorted(target.iterdir()):
            rel = str(p.relative_to(WORKSPACE_DIR))
            entries.append({
                "name": p.name,
                "path": rel,
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else 0,
                "modified": p.stat().st_mtime if p.exists() else 0,
            })
        return entries
    except Exception as exc:
        log.warning("list_files error: %s", exc)
        return []


def read_file(path: str) -> Tuple[bool, str]:
    try:
        p = _safe_path(path)
        if not p.is_file():
            return False, "Not a file"
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            data = base64.b64encode(p.read_bytes()).decode()
            return True, f"data:{mimetypes.guess_type(str(p))[0]};base64,{data}"
        text = p.read_text(encoding="utf-8", errors="replace")
        return True, text
    except Exception as exc:
        return False, str(exc)


def save_upload(file_obj: Any, filename: str, subpath: str = "") -> Tuple[bool, str]:
    try:
        safe_name = secure_filename(filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return False, f"Extension {ext} not allowed"
        dest_dir = _safe_path(subpath) if subpath else UPLOAD_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        file_obj.save(str(dest))
        rel = str(dest.relative_to(WORKSPACE_DIR))
        with _files_lock:
            _file_meta[rel] = {"name": safe_name, "path": rel, "size": dest.stat().st_size, "uploaded": _now_iso()}
        return True, rel
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Agent system
# ---------------------------------------------------------------------------
AGENT_DEFINITIONS: Dict[str, Dict] = {
    "architect": {
        "name": "architect",
        "label": "Architect Agent",
        "role": "senior_architect",
        "description": "Designs system architecture, technical specifications, and project structure.",
        "system_prompt": (
            "You are a Senior Software Architect. Your role is to design robust, scalable, and maintainable "
            "system architectures. Analyze requirements, identify trade-offs, propose architecture diagrams, "
            "technology choices, and implementation roadmaps. Be precise, structured, and consider security, "
            "scalability, and maintainability in every response."
        ),
        "permissions": ["read_code", "write_specs", "suggest_structure"],
        "tools": ["search", "rag", "code_analysis"],
        "status": "ready",
    },
    "reviewer": {
        "name": "reviewer",
        "label": "Reviewer Agent",
        "role": "code_reviewer",
        "description": "Reviews code for quality, security, and correctness.",
        "system_prompt": (
            "You are an expert Code Reviewer. Your role is to review code for correctness, security vulnerabilities, "
            "performance issues, and maintainability. Provide detailed, constructive feedback with specific line "
            "references and suggested improvements. Always check for: SQL injection, XSS, CSRF, authentication "
            "flaws, input validation, error handling, and code style compliance."
        ),
        "permissions": ["read_code", "write_review"],
        "tools": ["code_analysis", "security_scan"],
        "status": "ready",
    },
    "executor": {
        "name": "executor",
        "label": "Executor Agent",
        "role": "task_executor",
        "description": "Executes code, runs tests, and manages tasks.",
        "system_prompt": (
            "You are a Task Executor Agent. Your role is to execute code, run test suites, coordinate task "
            "pipelines, and report results. Provide clear, structured output including stdout, stderr, exit codes, "
            "and execution summaries. Handle errors gracefully and provide actionable diagnostics."
        ),
        "permissions": ["execute_code", "read_files", "write_files", "run_terminal"],
        "tools": ["terminal", "python_exec", "file_manager"],
        "status": "ready",
    },
    "github": {
        "name": "github",
        "label": "GitHub Agent",
        "role": "vcs_manager",
        "description": "Manages Git operations, branches, commits, and pull requests.",
        "system_prompt": (
            "You are a GitHub Integration Agent. Your role is to manage version control operations including "
            "creating branches, committing changes, pushing code, creating pull requests, and managing releases. "
            "Always follow conventional commit message format and provide clear descriptions of changes."
        ),
        "permissions": ["git_read", "git_write", "github_api"],
        "tools": ["git", "github_api"],
        "status": "ready",
    },
    "research": {
        "name": "research",
        "label": "Research Agent",
        "role": "researcher",
        "description": "Searches the web, summarizes findings, and compiles research reports.",
        "system_prompt": (
            "You are a Research Agent. Your role is to search for information, analyze sources, and compile "
            "comprehensive research reports. Cite your sources, evaluate credibility, identify conflicting "
            "information, and present findings in a clear, structured format with executive summaries."
        ),
        "permissions": ["web_search", "read_documents"],
        "tools": ["search", "web_fetch", "rag"],
        "status": "ready",
    },
    "rag": {
        "name": "rag",
        "label": "RAG Agent",
        "role": "knowledge_retrieval",
        "description": "Retrieves relevant context from the knowledge base for grounded answers.",
        "system_prompt": (
            "You are a RAG (Retrieval-Augmented Generation) Agent. Your role is to retrieve relevant information "
            "from the knowledge base, synthesize context from multiple sources, and provide grounded, accurate "
            "answers with source citations. Always indicate which sources you used and your confidence level."
        ),
        "permissions": ["read_knowledge_base", "search_vectors"],
        "tools": ["rag", "search"],
        "status": "ready",
    },
    "deployment": {
        "name": "deployment",
        "label": "Deployment Agent",
        "role": "devops",
        "description": "Manages deployments, infrastructure, and CI/CD pipelines.",
        "system_prompt": (
            "You are a Deployment Agent. Your role is to manage application deployments across environments "
            "(development, staging, production). Handle Docker builds, Kubernetes manifests, Railway deployments, "
            "environment configuration, health checks, and rollback procedures. Prioritize zero-downtime deployments "
            "and comprehensive monitoring."
        ),
        "permissions": ["read_config", "write_config", "deploy", "monitor"],
        "tools": ["terminal", "docker", "kubernetes", "railway"],
        "status": "ready",
    },
}

_jobs: Dict[str, Dict] = {}
_jobs_lock = threading.Lock()


def run_agent(agent_name: str, task: str, user_id: str, conv_id: str = "", extra_context: str = "") -> Dict:
    """Run an agent and return a job record."""
    agent_def = AGENT_DEFINITIONS.get(agent_name)
    if not agent_def:
        return {"ok": False, "error": f"Unknown agent: {agent_name}"}

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "agent": agent_name,
        "task": task,
        "status": "running",
        "started": _now_iso(),
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    def _run() -> None:
        try:
            messages = [
                {"role": "system", "content": agent_def["system_prompt"]},
            ]
            if extra_context:
                messages.append({"role": "user", "content": f"Context:\n{extra_context}"})
                messages.append({"role": "assistant", "content": "I understand the context. Ready to proceed."})

            # RAG agent: prepend retrieved context
            if agent_name == "rag":
                ctx = RAG.build_context(task)
                if ctx:
                    messages.append({"role": "system", "content": f"Retrieved context:\n{ctx}"})

            messages.append({"role": "user", "content": task})

            provider = REGISTRY.active_provider
            model = REGISTRY.active_model
            ok, reply, tokens = provider.chat(messages, model)
            track_request(provider=REGISTRY._active_name, model=model, tokens=tokens, error=not ok)

            with _jobs_lock:
                _jobs[job_id]["status"] = "done" if ok else "error"
                _jobs[job_id]["result"] = reply if ok else None
                _jobs[job_id]["error"] = None if ok else reply
                _jobs[job_id]["finished"] = _now_iso()
                _jobs[job_id]["tokens"] = tokens

            if conv_id:
                append_message(conv_id, "assistant", f"**[{agent_def['label']}]**\n\n{reply}", model=model, provider=REGISTRY._active_name, tokens=tokens)

        except Exception as exc:
            log.exception("Agent %s job %s failed", agent_name, job_id)
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(exc)
                _jobs[job_id]["finished"] = _now_iso()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id, "agent": agent_name}


# ---------------------------------------------------------------------------
# Plugin Registry
# ---------------------------------------------------------------------------
class PluginBase(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    plugin_type: str = "function"  # function | prompt_template | openapi | mcp

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        """Execute the plugin with given parameters."""

    def schema(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "type": self.plugin_type,
        }


class WebSearchPlugin(PluginBase):
    name = "web_search"
    description = "Search the web using DuckDuckGo and return results with titles and URLs."
    plugin_type = "function"

    def execute(self, query: str = "", max_results: int = 5, **kwargs) -> Any:
        return web_search(query, max_results)


class PythonExecPlugin(PluginBase):
    name = "python_exec"
    description = "Execute a Python code snippet in a sandboxed subprocess and return output."
    plugin_type = "function"

    def execute(self, code: str = "", timeout: int = 10, **kwargs) -> Any:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=timeout
            )
            return {
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Execution timed out", "returncode": -1}
        except Exception as exc:
            return {"error": str(exc), "returncode": -1}


class _PluginRegistry:
    def __init__(self) -> None:
        self._plugins: Dict[str, PluginBase] = {}
        self.register(WebSearchPlugin())
        self.register(PythonExecPlugin())

    def register(self, plugin: PluginBase) -> None:
        self._plugins[plugin.name] = plugin
        log.info("Plugin registered: %s", plugin.name)

    def unregister(self, name: str) -> bool:
        if name in self._plugins:
            del self._plugins[name]
            return True
        return False

    def execute(self, name: str, **kwargs) -> Any:
        plugin = self._plugins.get(name)
        if not plugin:
            return {"error": f"Plugin '{name}' not found"}
        try:
            return plugin.execute(**kwargs)
        except Exception as exc:
            return {"error": str(exc)}

    def list_plugins(self) -> List[Dict]:
        return [p.schema() for p in self._plugins.values()]


PLUGINS = _PluginRegistry()

# ---------------------------------------------------------------------------
# Terminal handler
# ---------------------------------------------------------------------------
def terminal_exec(cmd: str, cwd: str = None, timeout: int = 30) -> Generator[str, None, None]:
    """Execute command and stream stdout/stderr as NDJSON lines."""
    safe_cwd = str(WORKSPACE_DIR)
    if cwd:
        try:
            candidate = _safe_path(cwd)
            if candidate.is_dir():
                safe_cwd = str(candidate)
        except ValueError:
            pass

    # Block dangerous commands
    blocked = ["rm -rf /", "mkfs", ":(){:|:&};:", "dd if=/dev/zero"]
    for b in blocked:
        if b in cmd:
            yield json.dumps({"stream": "stderr", "line": f"Command blocked: {b}"}) + "\n"
            yield json.dumps({"stream": "exit", "code": 1}) + "\n"
            return

    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=safe_cwd, env={**os.environ}
        )

        def _read_stream(stream, label: str, q: queue.Queue) -> None:
            for line in stream:
                q.put({"stream": label, "line": line.rstrip()})
            q.put(None)  # sentinel

        q: queue.Queue = queue.Queue()
        t_out = threading.Thread(target=_read_stream, args=(proc.stdout, "stdout", q), daemon=True)
        t_err = threading.Thread(target=_read_stream, args=(proc.stderr, "stderr", q), daemon=True)
        t_out.start()
        t_err.start()

        sentinels = 0
        deadline = time.time() + timeout
        while sentinels < 2 and time.time() < deadline:
            try:
                item = q.get(timeout=0.2)
                if item is None:
                    sentinels += 1
                else:
                    yield json.dumps(item) + "\n"
            except queue.Empty:
                continue

        proc.wait(timeout=2)
        yield json.dumps({"stream": "exit", "code": proc.returncode}) + "\n"
    except subprocess.TimeoutExpired:
        yield json.dumps({"stream": "stderr", "line": "Command timed out"}) + "\n"
        yield json.dumps({"stream": "exit", "code": -1}) + "\n"
    except Exception as exc:
        yield json.dumps({"stream": "stderr", "line": str(exc)}) + "\n"
        yield json.dumps({"stream": "exit", "code": -1}) + "\n"


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------
def web_search(query: str, max_results: int = 5) -> Dict:
    """Search via DuckDuckGo Instant Answer API (no key required)."""
    try:
        params = {"q": query, "format": "json", "no_redirect": "1", "no_html": "1", "skip_disambig": "1"}
        r = requests.get("https://api.duckduckgo.com/", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = []
        # Abstract result
        if data.get("Abstract"):
            results.append({
                "title": data.get("Heading", query),
                "snippet": data["Abstract"],
                "url": data.get("AbstractURL", ""),
                "source": data.get("AbstractSource", ""),
            })
        # Related topics
        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:100],
                    "snippet": topic.get("Text", ""),
                    "url": topic.get("FirstURL", ""),
                    "source": "DuckDuckGo",
                })
        return {"ok": True, "query": query, "results": results[:max_results]}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "results": []}


# ---------------------------------------------------------------------------
# GitHub integration
# ---------------------------------------------------------------------------
def github_push(message: str = "chore: update from agent system") -> Dict:
    """git add . && git commit && git push using GITHUB_TOKEN."""
    if not GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN not configured"}

    results = []

    def _run(cmd: str) -> Tuple[bool, str]:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=str(Path(__file__).parent)
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output

    # Configure git credentials
    remote_url = ""
    ok, remote = _run("git remote get-url origin")
    if ok and remote:
        remote_url = remote.strip()
        if remote_url.startswith("https://") and GITHUB_TOKEN:
            # Inject token into URL
            auth_url = remote_url.replace("https://", f"https://x-token:{GITHUB_TOKEN}@")
            _run(f"git remote set-url origin {auth_url}")

    _run('git config user.email "agent@agent-system.local"')
    _run('git config user.name "Agent System"')

    ok1, out1 = _run("git add -A")
    results.append({"step": "git add", "ok": ok1, "output": out1})

    ok2, out2 = _run(f'git commit -m "{message}" --allow-empty')
    results.append({"step": "git commit", "ok": ok2, "output": out2})

    ok3, out3 = _run("git push")
    results.append({"step": "git push", "ok": ok3, "output": out3})

    # Restore original remote URL
    if remote_url:
        _run(f"git remote set-url origin {remote_url}")

    all_ok = all(r["ok"] for r in results[1:])  # commit/push must succeed
    return {"ok": all_ok, "steps": results}


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
ROLES = {
    "admin": {"level": 100, "permissions": ["*"]},
    "user": {"level": 10, "permissions": ["chat", "files", "agents", "search"]},
    "viewer": {"level": 1, "permissions": ["chat"]},
}

_api_keys: Dict[str, Dict] = {}
_api_keys_lock = threading.Lock()


def create_api_key(user_id: str, role: str = "user", label: str = "") -> str:
    key = "ys_" + secrets.token_hex(32)
    with _api_keys_lock:
        _api_keys[key] = {"user_id": user_id, "role": role, "label": label, "created": _now_iso()}
    return key


def validate_api_key(key: str) -> Optional[Dict]:
    with _api_keys_lock:
        return _api_keys.get(key)


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["JSON_SORT_KEYS"] = False


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Agent System — Login</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
  input{background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:6px;padding:8px 12px;width:100%;}
  input:focus{outline:none;border-color:#388bfd;box-shadow:0 0 0 3px rgba(56,139,253,.25);}
  .btn{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:6px;padding:10px;cursor:pointer;width:100%;}
  .btn:hover{background:#2ea043;}
  .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2rem;width:100%;max-width:400px;}
  .err{color:#f85149;background:#2d1717;border:1px solid #6e1212;border-radius:6px;padding:8px 12px;margin-bottom:1rem;}
  .logo{width:40px;height:40px;border-radius:8px;background:linear-gradient(135deg,#238636,#388bfd);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px;}
</style>
</head>
<body class="flex items-center justify-center min-h-screen">
<div class="card">
  <div class="flex items-center gap-3 mb-6">
    <div class="logo">AS</div>
    <div>
      <div class="font-semibold text-lg">Agent System</div>
      <div class="text-sm" style="color:#8b949e">AI Platform</div>
    </div>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post" action="/login">
    <div class="mb-4">
      <label class="block text-sm mb-1" style="color:#8b949e">Username</label>
      <input name="username" required autocomplete="username" placeholder="Enter username"/>
    </div>
    <div class="mb-6">
      <label class="block text-sm mb-1" style="color:#8b949e">Password</label>
      <input name="password" type="password" required autocomplete="current-password" placeholder="Enter password"/>
    </div>
    <button type="submit" class="btn">Sign in</button>
  </form>
</div>
</body>
</html>"""


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # Check session
        if session.get("logged_in"):
            return view(*args, **kwargs)
        # Check API key header
        api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if api_key:
            key_data = validate_api_key(api_key)
            if key_data:
                return view(*args, **kwargs)
        # For API routes return JSON 401
        if request.path.startswith("/api/") or request.path.startswith("/v1/"):
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapped


def current_user_id() -> str:
    return session.get("user_id", "anonymous")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if not YS_USER or not YS_PASSWORD:
            error = "Server not configured (YS_USER / YS_PASSWORD missing)."
        elif username == YS_USER and password == YS_PASSWORD:
            session.clear()
            session["logged_in"] = True
            session["user_id"] = uuid.uuid4().hex
            session["username"] = username
            session["role"] = "admin"
            log.info("Login: %s", username)
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        else:
            error = "Invalid credentials."
            log.warning("Failed login attempt for user: %s", username)
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    log.info("Logout: %s", session.get("username", "unknown"))
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# SPA routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    p = Path(__file__).parent / "index.html"
    if p.exists():
        return send_from_directory(str(p.parent), "index.html")
    return "<h1>Agent System</h1><p>index.html not found.</p>", 404


@app.route("/<path:subpath>")
@login_required
def spa(subpath: str):
    candidate = Path(__file__).parent / subpath
    if candidate.exists() and candidate.is_file():
        return send_from_directory(str(candidate.parent), candidate.name)
    p = Path(__file__).parent / "index.html"
    if p.exists():
        return send_from_directory(str(p.parent), "index.html")
    return "Not Found", 404


# ---------------------------------------------------------------------------
# Health & logs & trigger
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": _now_iso(),
        "provider": REGISTRY._active_name,
        "model": REGISTRY.active_model,
        "rag_backend": RAG.backend_name,
        "workspace": str(WORKSPACE_DIR),
    })


@app.route("/api/logs")
@login_required
def api_logs():
    level = request.args.get("level", "").upper()
    limit = min(int(request.args.get("limit", 200)), 2000)
    with _log_lock:
        records = list(LOG_RECORDS)
    if level:
        records = [r for r in records if r.get("level") == level]
    return jsonify({"ok": True, "logs": records[-limit:], "total": len(records)})


@app.route("/api/trigger", methods=["POST"])
@login_required
def api_trigger():
    """Trigger an agent pipeline run."""
    data = request.get_json(silent=True) or {}
    pipeline = data.get("pipeline", "default")
    task = data.get("task", "Analyze the current workspace and provide a status report.")
    agents_to_run = data.get("agents", ["architect", "reviewer"])
    conv_id = data.get("conversation_id", "")
    user_id = current_user_id()

    if not conv_id:
        conv = create_conversation(user_id, title=f"Pipeline: {pipeline}")
        conv_id = conv["id"]

    jobs = []
    prev_result = ""
    for agent_name in agents_to_run:
        result = run_agent(agent_name, task, user_id, conv_id, extra_context=prev_result)
        if result.get("ok"):
            jobs.append(result)
    log.info("Triggered pipeline '%s' with agents %s", pipeline, agents_to_run)
    return jsonify({"ok": True, "pipeline": pipeline, "jobs": jobs, "conversation_id": conv_id})


# ---------------------------------------------------------------------------
# Provider & model routes
# ---------------------------------------------------------------------------
@app.route("/api/providers")
@login_required
def api_providers():
    return jsonify({"ok": True, "providers": REGISTRY.list_providers()})


@app.route("/api/providers/switch", methods=["POST"])
@login_required
def api_providers_switch():
    data = request.get_json(silent=True) or {}
    provider_name = data.get("provider")
    model = data.get("model", "")
    if not provider_name:
        return jsonify({"ok": False, "error": "provider required"}), 400
    if not REGISTRY.set_active(provider_name, model):
        return jsonify({"ok": False, "error": f"Unknown provider: {provider_name}"}), 400
    log.info("Switched provider to %s / %s", provider_name, REGISTRY.active_model)
    return jsonify({"ok": True, "provider": REGISTRY._active_name, "model": REGISTRY.active_model})


@app.route("/api/models")
@login_required
def api_models():
    provider_name = request.args.get("provider", REGISTRY._active_name)
    try:
        models = REGISTRY.list_models(provider_name)
    except Exception as exc:
        models = []
        log.warning("Model list failed for %s: %s", provider_name, exc)
    return jsonify({
        "ok": True,
        "provider": provider_name,
        "models": models,
        "active": REGISTRY.active_model,
    })


@app.route("/api/models/switch", methods=["POST"])
@login_required
def api_models_switch():
    data = request.get_json(silent=True) or {}
    model = data.get("model", "")
    if not model:
        return jsonify({"ok": False, "error": "model required"}), 400
    REGISTRY.active_model = model
    log.info("Switched model to %s", model)
    return jsonify({"ok": True, "model": REGISTRY.active_model})


# ---------------------------------------------------------------------------
# Chat routes
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400

    user_id = current_user_id()
    conv_id = data.get("conversation_id")
    model = data.get("model") or REGISTRY.active_model
    provider_name = data.get("provider") or REGISTRY._active_name
    use_rag = data.get("use_rag", False)

    # Resolve conversation
    if not conv_id:
        conv = create_conversation(user_id)
        conv_id = conv["id"]
    else:
        conv = get_conversation(conv_id)
        if conv is None:
            conv = create_conversation(user_id)
            conv_id = conv["id"]

    append_message(conv_id, "user", message)

    # Build messages list
    messages: List[Dict] = []

    # System prompt
    agent_name = data.get("agent")
    if agent_name and agent_name in AGENT_DEFINITIONS:
        messages.append({"role": "system", "content": AGENT_DEFINITIONS[agent_name]["system_prompt"]})

    # RAG context
    if use_rag:
        ctx = RAG.build_context(message)
        if ctx:
            messages.append({"role": "system", "content": f"Use the following context to answer:\n\n{ctx}"})

    # Conversation history (last 20 turns)
    conv = get_conversation(conv_id)
    history = (conv or {}).get("messages", [])
    for m in history[-40:]:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})

    provider = REGISTRY.get(provider_name)
    ok, reply, tokens = provider.chat(messages, model)
    track_request(provider=provider_name, model=model, tokens=tokens, error=not ok)

    if not ok:
        log.warning("Chat error: %s", reply)
        return jsonify({"ok": False, "error": reply, "conversation_id": conv_id}), 502

    append_message(conv_id, "assistant", reply, model=model, provider=provider_name, tokens=tokens)
    return jsonify({"ok": True, "reply": reply, "conversation_id": conv_id, "tokens": tokens})


@app.route("/api/chat/stream", methods=["POST"])
@login_required
def api_chat_stream():
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not message:
        def _err():
            yield f"data: {json.dumps({'type':'error','error':'message required'})}\n\n"
        return Response(stream_with_context(_err()), content_type="text/event-stream")

    user_id = current_user_id()
    conv_id = data.get("conversation_id")
    model = data.get("model") or REGISTRY.active_model
    provider_name = data.get("provider") or REGISTRY._active_name
    use_rag = data.get("use_rag", False)

    if not conv_id:
        conv = create_conversation(user_id)
        conv_id = conv["id"]
    else:
        conv = get_conversation(conv_id)
        if conv is None:
            conv = create_conversation(user_id)
            conv_id = conv["id"]

    append_message(conv_id, "user", message)

    messages: List[Dict] = []
    agent_name = data.get("agent")
    if agent_name and agent_name in AGENT_DEFINITIONS:
        messages.append({"role": "system", "content": AGENT_DEFINITIONS[agent_name]["system_prompt"]})

    if use_rag:
        ctx = RAG.build_context(message)
        if ctx:
            messages.append({"role": "system", "content": f"Use the following context to answer:\n\n{ctx}"})

    conv = get_conversation(conv_id)
    history = (conv or {}).get("messages", [])
    for m in history[-40:]:
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})

    provider = REGISTRY.get(provider_name)

    def _generate():
        full_text = []
        try:
            yield f"data: {json.dumps({'type':'start','conversation_id':conv_id})}\n\n"
            for chunk in provider.stream_chat(messages, model):
                full_text.append(chunk)
                yield f"data: {json.dumps({'type':'partial','text':chunk,'conversation_id':conv_id})}\n\n"
            complete = "".join(full_text)
            tokens = max(1, len(complete) // 4)
            track_request(provider=provider_name, model=model, tokens=tokens)
            append_message(conv_id, "assistant", complete, model=model, provider=provider_name, tokens=tokens)
            yield f"data: {json.dumps({'type':'done','conversation_id':conv_id,'tokens':tokens})}\n\n"
        except Exception as exc:
            log.exception("Streaming error")
            track_request(provider=provider_name, model=model, error=True)
            yield f"data: {json.dumps({'type':'error','error':str(exc)})}\n\n"

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Conversation routes
# ---------------------------------------------------------------------------
@app.route("/api/conversations", methods=["GET"])
@login_required
def api_conversations_list():
    user_id = current_user_id()
    convs = get_conversations(user_id)
    result = []
    for c in convs:
        result.append({
            "id": c["id"],
            "title": c["title"],
            "folder": c.get("folder", ""),
            "tags": c.get("tags", []),
            "favorite": c.get("favorite", False),
            "updated": c.get("updated", 0),
            "created": c.get("created", 0),
            "message_count": len(c.get("messages", [])),
        })
    return jsonify({"ok": True, "conversations": result})


@app.route("/api/conversations", methods=["POST"])
@login_required
def api_conversations_create():
    data = request.get_json(silent=True) or {}
    user_id = current_user_id()
    conv = create_conversation(
        user_id,
        title=data.get("title", "New Chat"),
        folder=data.get("folder", ""),
        tags=data.get("tags", []),
    )
    return jsonify({"ok": True, "conversation": {
        "id": conv["id"],
        "title": conv["title"],
        "folder": conv["folder"],
        "tags": conv["tags"],
        "favorite": conv["favorite"],
        "updated": conv["updated"],
        "messages": [],
    }})


@app.route("/api/conversation/<conv_id>", methods=["GET"])
@login_required
def api_conversation_get(conv_id: str):
    conv = get_conversation(conv_id)
    if conv is None:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "conversation": conv})


@app.route("/api/conversation/<conv_id>", methods=["PATCH"])
@login_required
def api_conversation_update(conv_id: str):
    data = request.get_json(silent=True) or {}
    allowed = {"title", "folder", "tags", "favorite"}
    fields = {k: v for k, v in data.items() if k in allowed}
    updated = update_conversation(conv_id, **fields)
    if updated is None:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "conversation": updated})


@app.route("/api/conversation/<conv_id>", methods=["DELETE"])
@login_required
def api_conversation_delete(conv_id: str):
    user_id = current_user_id()
    ok = delete_conversation(conv_id, user_id)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Agent routes
# ---------------------------------------------------------------------------
@app.route("/api/agents")
@login_required
def api_agents_list():
    agents = [
        {k: v for k, v in a.items() if k != "system_prompt"}
        for a in AGENT_DEFINITIONS.values()
    ]
    return jsonify({"ok": True, "agents": agents})


@app.route("/api/agents/<name>/run", methods=["POST"])
@login_required
def api_agent_run(name: str):
    data = request.get_json(silent=True) or {}
    task = data.get("task", "")
    if not task:
        return jsonify({"ok": False, "error": "task required"}), 400
    user_id = current_user_id()
    conv_id = data.get("conversation_id", "")
    extra = data.get("context", "")
    result = run_agent(name, task, user_id, conv_id, extra)
    return jsonify(result)


@app.route("/api/jobs/<job_id>")
@login_required
def api_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, "job": job})


# ---------------------------------------------------------------------------
# File routes
# ---------------------------------------------------------------------------
@app.route("/api/files/list")
@login_required
def api_files_list():
    path = request.args.get("path", "")
    files = list_files(path)
    return jsonify({"ok": True, "files": files, "path": path})


@app.route("/api/files/read")
@login_required
def api_files_read():
    path = request.args.get("path", "")
    ok, content = read_file(path)
    if not ok:
        return jsonify({"ok": False, "error": content}), 400
    return jsonify({"ok": True, "path": path, "text": content})


@app.route("/api/files/upload", methods=["POST"])
@login_required
def api_files_upload():
    subpath = request.args.get("path", "")
    saved = []
    errors = []
    for key in request.files:
        f = request.files[key]
        if not f.filename:
            continue
        ok, result = save_upload(f, f.filename, subpath)
        if ok:
            saved.append(result)
            # Ingest text files into RAG
            try:
                full_path = WORKSPACE_DIR / result
                if full_path.suffix in {".txt", ".md", ".py", ".js", ".ts", ".json"}:
                    text = full_path.read_text(encoding="utf-8", errors="replace")
                    chunks = RAG.ingest_text(text, {"source": result})
                    log.info("RAG ingested %d chunks from %s", chunks, result)
            except Exception:
                pass
        else:
            errors.append({"file": f.filename, "error": result})
    return jsonify({"ok": bool(saved), "saved": saved, "errors": errors})


@app.route("/api/files/delete", methods=["DELETE"])
@login_required
def api_files_delete():
    path = request.args.get("path", "")
    try:
        p = _safe_path(path)
        if not p.exists():
            return jsonify({"ok": False, "error": "Not found"}), 404
        if p.is_dir():
            shutil.rmtree(str(p))
        else:
            p.unlink()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ---------------------------------------------------------------------------
# Terminal route
# ---------------------------------------------------------------------------
@app.route("/api/terminal/exec", methods=["POST"])
@login_required
def api_terminal_exec():
    data = request.get_json(silent=True) or {}
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return jsonify({"ok": False, "error": "cmd required"}), 400
    cwd = data.get("cwd", "")
    timeout = min(int(data.get("timeout", 30)), 120)
    log.info("Terminal exec: %s", cmd[:200])

    return Response(
        stream_with_context(terminal_exec(cmd, cwd, timeout)),
        content_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Plugin routes
# ---------------------------------------------------------------------------
@app.route("/api/plugins")
@login_required
def api_plugins_list():
    return jsonify({"ok": True, "plugins": PLUGINS.list_plugins()})


@app.route("/api/plugins/<name>/run", methods=["POST"])
@login_required
def api_plugin_run(name: str):
    data = request.get_json(silent=True) or {}
    result = PLUGINS.execute(name, **data)
    return jsonify({"ok": True, "result": result})


# ---------------------------------------------------------------------------
# Search route
# ---------------------------------------------------------------------------
@app.route("/api/search", methods=["POST"])
@login_required
def api_search():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    max_results = min(int(data.get("max_results", 5)), 20)
    result = web_search(query, max_results)
    return jsonify(result)


# ---------------------------------------------------------------------------
# RAG routes
# ---------------------------------------------------------------------------
@app.route("/api/rag/search", methods=["POST"])
@login_required
def api_rag_search():
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400
    top_k = min(int(data.get("top_k", 5)), 20)
    results = RAG.search(query, top_k)
    return jsonify({"ok": True, "query": query, "results": results})


@app.route("/api/rag/ingest", methods=["POST"])
@login_required
def api_rag_ingest():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    metadata = data.get("metadata", {})
    chunks = RAG.ingest_text(text, metadata)
    log.info("RAG ingested %d chunks", chunks)
    return jsonify({"ok": True, "chunks": chunks})


# ---------------------------------------------------------------------------
# GitHub route
# ---------------------------------------------------------------------------
@app.route("/api/github/push", methods=["POST"])
@login_required
def api_github_push():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "chore: update from agent system")
    result = github_push(message)
    if result.get("ok"):
        log.info("GitHub push successful")
    else:
        log.warning("GitHub push failed: %s", result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route("/api/admin/stats")
@login_required
def api_admin_stats():
    with _stats_lock:
        stats_copy = dict(_stats)
    with _conv_lock:
        total_convs = len(_conversations)
    with _jobs_lock:
        total_jobs = len(_jobs)
    stats_copy["conversations_total"] = total_convs
    stats_copy["jobs_total"] = total_jobs
    stats_copy["plugins_total"] = len(PLUGINS.list_plugins())
    stats_copy["agents_total"] = len(AGENT_DEFINITIONS)
    stats_copy["rag_backend"] = RAG.backend_name
    return jsonify({"ok": True, "stats": stats_copy})


@app.route("/api/admin/usage")
@login_required
def api_admin_usage():
    with _stats_lock:
        provider_usage = dict(_stats.get("provider_usage", {}))
        model_usage = dict(_stats.get("model_usage", {}))
    return jsonify({"ok": True, "provider_usage": provider_usage, "model_usage": model_usage})


# ---------------------------------------------------------------------------
# Compare models (multi-model chat)
# ---------------------------------------------------------------------------
@app.route("/api/compare", methods=["POST"])
@login_required
def api_compare():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    targets = data.get("targets", [])  # [{provider, model}]
    if not targets:
        return jsonify({"ok": False, "error": "targets required"}), 400

    messages = [{"role": "user", "content": message}]
    results = []
    for t in targets:
        p_name = t.get("provider", REGISTRY._active_name)
        m = t.get("model", REGISTRY.active_model)
        provider = REGISTRY.get(p_name)
        ok, reply, tokens = provider.chat(messages, m)
        track_request(provider=p_name, model=m, tokens=tokens, error=not ok)
        results.append({
            "provider": p_name,
            "model": m,
            "ok": ok,
            "reply": reply if ok else None,
            "error": reply if not ok else None,
            "tokens": tokens,
        })
    return jsonify({"ok": True, "results": results})


# ---------------------------------------------------------------------------
# OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
@app.route("/v1/chat/completions", methods=["POST"])
@login_required
def v1_chat_completions():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": {"message": "messages required", "type": "invalid_request_error"}}), 400
    model = data.get("model") or REGISTRY.active_model
    stream = data.get("stream", False)
    provider = REGISTRY.active_provider

    if stream:
        def _gen():
            cid = f"chatcmpl-{uuid.uuid4().hex}"
            for chunk in provider.stream_chat(messages, model):
                payload = {
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(_gen()), content_type="text/event-stream")

    ok, reply, tokens = provider.chat(messages, model)
    track_request(provider=REGISTRY._active_name, model=model, tokens=tokens, error=not ok)
    if not ok:
        return jsonify({"error": {"message": reply, "type": "provider_error"}}), 502
    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        "usage": {"total_tokens": tokens},
    })


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def err_404(e):
    if request.path.startswith("/api/") or request.path.startswith("/v1/"):
        return jsonify({"ok": False, "error": "Not found"}), 404
    return redirect(url_for("index"))


@app.errorhandler(413)
def err_413(e):
    return jsonify({"ok": False, "error": f"File too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)"}), 413


@app.errorhandler(500)
def err_500(e):
    log.exception("Internal server error")
    return jsonify({"ok": False, "error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=== Agent System starting ===")
    log.info("Host: %s  Port: %s", HOST, PORT)
    log.info("Provider: %s  Model: %s", REGISTRY._active_name, REGISTRY.active_model)
    log.info("Ollama base: %s", OLLAMA_BASE)
    log.info("Workspace: %s", WORKSPACE_DIR)

    if not YS_USER or not YS_PASSWORD:
        log.warning("YS_USER / YS_PASSWORD not set — login will fail")
    if not GITHUB_TOKEN:
        log.info("GITHUB_TOKEN not set — GitHub push disabled")

    # Test Ollama connectivity
    try:
        models = REGISTRY.list_models("ollama")
        log.info("Ollama models available: %s", models)
    except Exception as exc:
        log.warning("Ollama not reachable: %s", exc)

    app.run(host=HOST, port=PORT, threaded=True, debug=False)
