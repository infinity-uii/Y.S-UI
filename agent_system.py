#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_system.py

Flask backend for Agent System with Ollama integration.

Features:
- Environment-based login (YS_USER / YS_PASSWORD)
- Ollama model detection (prefers llama3:latest, llama3.2:latest, llama3.2:1b)
- Model selection/switching API
- OpenAI-compatible /v1/chat/completions endpoint
- Simple conversation storage per-session
- Robust error handling and graceful fallbacks (CLI and HTTP)
- Minimal dependencies: Flask, requests

Usage:
  pip install flask requests
  export YS_USER=youruser
  export YS_PASSWORD=yourpass
  python agent_system.py

Open http://localhost:8080/ and login.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import (
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
    jsonify,
)

# -----------------------
# Configuration
# -----------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
YS_USER = os.environ.get("YS_USER")
YS_PASSWORD = os.environ.get("YS_PASSWORD")
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())

OLLAMA_CLI = shutil.which("ollama")
OLLAMA_HTTP_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434").rstrip("/")

# Preferred model priority
MODEL_PRIORITY = [
    "llama3:latest",
    "llama3.2:latest",
    "llama3.2:1b",
]

# Flask app
app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config["JSON_SORT_KEYS"] = False

# Simple in-memory per-session conversation store
# session["user_id"] -> store in global store_map
_store_lock = threading.Lock()
_store_map: Dict[str, Dict[str, Any]] = {}

# Detected models and active model
_detected_models: List[str] = []
_active_model: Optional[str] = None


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# -----------------------
# Ollama client
# -----------------------
class OllamaClient:
    def __init__(self, cli_path: Optional[str] = OLLAMA_CLI, http_base: str = OLLAMA_HTTP_BASE):
        self.cli = cli_path
        self.http_base = http_base.rstrip("/")

    def _cli(self, args: List[str], timeout: float = 30.0) -> Tuple[bool, str]:
        """Run ollama CLI if available."""
        if not self.cli:
            return False, "ollama CLI not found"
        cmd = [self.cli] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                return False, err or out or f"exit {proc.returncode}"
            return True, out or err or ""
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    def _http_get(self, path: str, timeout: float = 5.0) -> Tuple[bool, Any]:
        url = f"{self.http_base}{path}"
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            try:
                return True, r.json()
            except Exception:
                return True, r.text
        except Exception as exc:
            return False, str(exc)

    def _http_post(self, path: str, payload: Any, timeout: float = 20.0) -> Tuple[bool, Any]:
        url = f"{self.http_base}{path}"
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            try:
                return True, r.json()
            except Exception:
                return True, r.text
        except Exception as exc:
            return False, str(exc)

    def detect_models(self) -> List[str]:
        """Detect local ollama models using CLI first, then HTTP."""
        models: List[str] = []
        # Try CLI with JSON output if supported
        ok, out = self._cli(["list", "--json"])
        if ok and out:
            try:
                parsed = json.loads(out)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and "name" in item:
                            models.append(item["name"])
                        elif isinstance(item, str):
                            models.append(item)
                elif isinstance(parsed, dict):
                    # some versions may wrap models
                    arr = parsed.get("models") or parsed.get("data") or []
                    for item in arr:
                        if isinstance(item, dict) and "name" in item:
                            models.append(item["name"])
                        elif isinstance(item, str):
                            models.append(item)
            except Exception:
                # fall through to text parsing
                pass
        if not models:
            # Try CLI plain output
            ok2, out2 = self._cli(["list"])
            if ok2 and out2:
                for line in out2.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # Typically name is first token
                    parts = line.split()
                    models.append(parts[0])
        # HTTP fallback
        if not models:
            ok3, data = self._http_get("/models")
            if ok3:
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item:
                            models.append(item["name"])
                        elif isinstance(item, str):
                            models.append(item)
                elif isinstance(data, dict):
                    arr = data.get("models") or data.get("data") or []
                    for item in arr:
                        if isinstance(item, dict) and "name" in item:
                            models.append(item["name"])
                        elif isinstance(item, str):
                            models.append(item)
        # Deduplicate preserving order
        seen = set()
        result: List[str] = []
        for m in models:
            if m and m not in seen:
                seen.add(m)
                result.append(m)
        return result

    def run_chat(self, model: str, messages: List[Dict[str, str]], timeout: float = 60.0) -> Tuple[bool, str]:
        """
        Try HTTP chat completions endpoints then fall back to CLI run.
        Returns (ok, reply_text_or_error).
        """
        payload = {"model": model, "messages": messages}
        endpoints = ["/v1/chat/completions", "/chat/completions", "/responses", "/generate"]
        for ep in endpoints:
            ok, resp = self._http_post(ep, payload, timeout=timeout)
            if not ok:
                continue
            # Parse common shapes
            try:
                if isinstance(resp, dict):
                    if "choices" in resp and isinstance(resp["choices"], list) and resp["choices"]:
                        choice = resp["choices"][0]
                        if isinstance(choice, dict):
                            msg = choice.get("message")
                            if isinstance(msg, dict) and "content" in msg:
                                return True, msg["content"]
                            if "text" in choice and isinstance(choice["text"], str):
                                return True, choice["text"]
                        if isinstance(choice, str):
                            return True, choice
                    if "text" in resp and isinstance(resp["text"], str):
                        return True, resp["text"]
                    if "output" in resp and isinstance(resp["output"], list):
                        texts = []
                        for item in resp["output"]:
                            if isinstance(item, dict):
                                c = item.get("content")
                                if isinstance(c, str):
                                    texts.append(c)
                        if texts:
                            return True, "\n".join(texts)
                    if "generated" in resp and isinstance(resp["generated"], list) and resp["generated"]:
                        gen = resp["generated"][0]
                        if isinstance(gen, dict) and "content" in gen:
                            return True, gen["content"]
                if isinstance(resp, str):
                    return True, resp
            except Exception:
                # continue to next endpoint
                pass
        # CLI fallback: construct prompt and run
        if self.cli:
            try:
                prompt_parts: List[str] = []
                for m in messages:
                    role = m.get("role", "user")
                    content = m.get("content", "")
                    prefix = "[SYSTEM]" if role == "system" else ("[ASSISTANT]" if role == "assistant" else "[USER]")
                    prompt_parts.append(f"{prefix} {content}")
                prompt = "\n".join(prompt_parts)
                # 'ollama run <model>' accepts stdin; using --json if available
                cmd = [self.cli, "run", model, "--json"]
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
                if proc.returncode == 0:
                    return True, stdout.strip()
                return False, (stderr or stdout).strip()
            except Exception as exc:
                return False, str(exc)
        return False, "No Ollama HTTP endpoint or CLI available"


# Singleton client
OLLAMA = OllamaClient()


# -----------------------
# Model detection and active selection
# -----------------------
def refresh_models_and_active() -> None:
    """Refresh detected models and set active according to priority."""
    global _detected_models, _active_model
    try:
        detected = OLLAMA.detect_models()
    except Exception:
        detected = []
    _detected_models = detected or []
    # Determine active model according to priority
    active = None
    for pref in MODEL_PRIORITY:
        if pref in _detected_models:
            active = pref
            break
    if active is None and _detected_models:
        active = _detected_models[0]
    _active_model = active


# Initial detection
refresh_models_and_active()


# -----------------------
# Helpers
# -----------------------
def run_with_model_fallback(messages: List[Dict[str, str]], preferred: Optional[str] = None) -> Tuple[bool, str]:
    """
    Try preferred model first (if provided), then MODEL_PRIORITY, then detected models.
    Returns (ok, reply_or_error).
    """
    tried = []
    order = []
    if preferred:
        order.append(preferred)
    for m in MODEL_PRIORITY:
        if m not in order:
            order.append(m)
    for m in _detected_models:
        if m not in order:
            order.append(m)
    for model in order:
        if not model:
            continue
        tried.append(model)
        ok, reply = OLLAMA.run_chat(model, messages)
        if ok:
            return True, reply
    return False, f"No models succeeded. Tried: {', '.join(tried)}"


def get_store_for_session() -> Dict[str, Any]:
    uid = session.get("user_id")
    if not uid:
        # create ephemeral id for unauthenticated access (shouldn't happen with login_required)
        uid = uuid.uuid4().hex
        session["user_id"] = uid
    with _store_lock:
        return _store_map.setdefault(uid, {"conversations": []})


# -----------------------
# Authentication & UI
# -----------------------
LOGIN_HTML = """
<!doctype html>
<html lang="en" class="dark">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Login</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100">
  <div class="flex items-center justify-center h-screen">
    <form method="post" action="/login" class="bg-gray-800 p-8 rounded w-full max-w-md">
      <h1 class="text-xl mb-4">Agent System — Login</h1>
      {% if error %}<div class="mb-3 text-red-400">{{ error }}</div>{% endif %}
      <label class="block">Username</label>
      <input name="username" class="w-full p-2 rounded mb-3 bg-gray-700" required />
      <label class="block">Password</label>
      <input name="password" type="password" class="w-full p-2 rounded mb-4 bg-gray-700" required />
      <button type="submit" class="w-full bg-indigo-600 p-2 rounded">Sign in</button>
    </form>
  </div>
</body>
</html>
"""


def login_required(view):
    """Decorator for routes requiring login."""
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if not YS_USER or not YS_PASSWORD:
            error = "Server not configured with YS_USER/YS_PASSWORD."
        elif username == YS_USER and password == YS_PASSWORD:
            session.clear()
            session["logged_in"] = True
            session["user_id"] = uuid.uuid4().hex
            session["username"] = username
            return redirect(url_for("index"))
        else:
            error = "Invalid credentials."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    # serve index.html if present
    idx = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(idx):
        return send_from_directory(os.path.dirname(idx), "index.html")
    return "<h1>Agent System</h1><p>Place an index.html in the project root.</p>"


@app.route("/<path:subpath>", methods=["GET"])
@login_required
def spa(subpath: str):
    # try to serve file, otherwise index.html for SPA
    candidate = os.path.join(os.path.dirname(__file__), subpath)
    if os.path.exists(candidate) and os.path.isfile(candidate):
        return send_from_directory(os.path.dirname(candidate), os.path.basename(candidate))
    idx = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(idx):
        return send_from_directory(os.path.dirname(idx), "index.html")
    return "Not Found", 404


# -----------------------
# API: models, chat, health
# -----------------------
@app.route("/api/models", methods=["GET"])
@login_required
def api_models():
    try:
        refresh_models_and_active()
        return jsonify({"ok": True, "detected": _detected_models, "active": _active_model})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/models/switch", methods=["POST"])
@login_required
def api_models_switch():
    global _active_model
    data = request.get_json(silent=True) or {}
    model = data.get("model")
    if not model:
        return jsonify({"ok": False, "error": "model required"}), 400
    refresh_models_and_active()
    if model not in _detected_models:
        return jsonify({"ok": False, "error": "model not available"}), 400
    _active_model = model
    return jsonify({"ok": True, "active": _active_model})


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    """
    Simple chat endpoint:
    payload: { conversation_id: optional, message: str, model: optional }
    """
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if message is None:
        return jsonify({"ok": False, "error": "message required"}), 400
    model = data.get("model") or _active_model
    store = get_store_for_session()
    conv_id = data.get("conversation_id")
    if not conv_id:
        conv_id = uuid.uuid4().hex
        conv = {"id": conv_id, "messages": [], "created": time.time()}
        store["conversations"].append(conv)
    else:
        conv = next((c for c in store["conversations"] if c["id"] == conv_id), None)
        if conv is None:
            conv = {"id": conv_id, "messages": [], "created": time.time()}
            store["conversations"].append(conv)
    user_msg = {"role": "user", "content": message, "time": now_iso()}
    conv["messages"].append(user_msg)
    messages = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in conv["messages"]]
    ok, reply = run_with_model_fallback(messages, preferred=model)
    if not ok:
        assistant_msg = {"role": "assistant", "content": f"[Error] {reply}", "time": now_iso()}
        conv["messages"].append(assistant_msg)
        return jsonify({"ok": False, "error": reply, "conversation_id": conv_id}), 502
    assistant_msg = {"role": "assistant", "content": reply, "time": now_iso()}
    conv["messages"].append(assistant_msg)
    return jsonify({"ok": True, "reply": reply, "conversation_id": conv_id})


@app.route("/v1/chat/completions", methods=["POST"])
@login_required
def v1_chat_completions():
    """
    OpenAI-compatible endpoint.
    Accepts: { model: optional, messages: [ {role, content}, ... ] }
    """
    data = request.get_json(silent=True) or {}
    messages = data.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages required"}), 400
    model = data.get("model") or _active_model
    ok, reply = run_with_model_fallback(messages, preferred=model)
    if not ok:
        return jsonify({"error": reply}), 502
    resp = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
    }
    return jsonify(resp)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": now_iso(), "active_model": _active_model, "detected_models": _detected_models})


# -----------------------
# Startup
# -----------------------
if __name__ == "__main__":
    # Basic sanity checks
    if not YS_USER or not YS_PASSWORD:
        print("WARNING: YS_USER or YS_PASSWORD not set. Login will fail until configured.", file=sys.stderr)
    try:
        refresh_models_and_active()
    except Exception:
        # ensure server still starts even if detection fails
        pass
    # Run Flask app
    app.run(host=HOST, port=PORT, threaded=True)
