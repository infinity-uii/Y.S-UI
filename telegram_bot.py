# Simple Telegram bot bridge (polling)
import os
import time
import requests
import threading
import logging
from typing import Optional

log = logging.getLogger("telegram_bot")

TELEGRAM_API = "https://api.telegram.org"


def _send_message(token: str, chat_id: int, text: str) -> None:
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text})
    except Exception as exc:
        log.exception("Failed to send telegram message: %s", exc)


def _call_chat_api(base_url: str, api_key: str, message: str) -> Optional[str]:
    url = base_url.rstrip("/") + "/api/chat"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json={"message": message}, timeout=30)
        if r.ok:
            data = r.json()
            return data.get("reply")
        else:
            return f"Error: {r.status_code} {r.text}"
    except Exception as exc:
        log.exception("Error calling chat API: %s", exc)
        return None


def run_bot(token: str, api_key: str, base_url: str = "http://127.0.0.1:8080"):
    """Run a simple polling Telegram bot that forwards /chat commands to the local chat API.
    Commands supported:
    - /chat <message> : send message to AI and return reply
    - /start : show help
    """
    last_update = 0
    log.info("Starting telegram bot polling")
    while True:
        try:
            url = f"{TELEGRAM_API}/bot{token}/getUpdates?timeout=10&offset={last_update + 1}"
            r = requests.get(url, timeout=30)
            if not r.ok:
                time.sleep(2)
                continue
            data = r.json()
            for item in data.get("result", []):
                last_update = max(last_update, item.get("update_id", 0))
                msg = item.get("message") or item.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if text.startswith("/start"):
                    _send_message(token, chat_id, "Agent System Bot. Use /chat <message> to talk to the AI.")
                    continue
                if text.startswith("/chat "):
                    prompt = text[len("/chat "):].strip()
                    _send_message(token, chat_id, "Processing...")
                    reply = _call_chat_api(base_url, api_key, prompt)
                    if reply is None:
                        _send_message(token, chat_id, "Error contacting chat API.")
                    else:
                        _send_message(token, chat_id, reply)
                    continue
                # fallback: echo help
                _send_message(token, chat_id, "Unknown command. Use /chat <message>")
        except Exception as exc:
            log.exception("Telegram bot polling failed: %s", exc)
            time.sleep(5)
