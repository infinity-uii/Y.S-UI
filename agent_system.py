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
# YS_USER / YS_PASSWORD are intentionally NOT cached at module load time.
# They are read from os.environ on every request so that secrets set after
# process start (e.g. via Replit Secrets) are picked up without a restart.
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

# enable CORS if available
try:
    from flask_cors import CORS

    app = Flask(__name__, static_folder=None)
    CORS(app, resources={r"/api/*": {"origins": "*"}})
except Exception:
    app = Flask(__name__, static_folder=None)

app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["JSON_SORT_KEYS"] = False

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


# [rest of file unchanged]
