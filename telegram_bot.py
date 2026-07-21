"""
telegram_bot.py — Full-featured Arabic Telegram Bot for Y.S Agent System.

Supports all platform features via inline keyboard menus:
- Chat with AI (multi-provider)
- Agent selection and execution
- Provider & model switching
- File workspace browsing
- Knowledge base (RAG) ingestion/search
- Admin stats & logs
- Conversation history
- Settings

Uses Arabic UI with RTL-friendly layout and inline keyboards.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("telegram_bot")
TELEGRAM_API = "https://api.telegram.org"

# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _tg(token: str, method: str, **kwargs) -> Optional[Dict]:
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=15)
        if r.ok:
            return r.json()
    except Exception as exc:
        log.error("Telegram API error [%s]: %s", method, exc)
    return None


def send_msg(token: str, chat_id: int, text: str,
             reply_markup: Optional[Dict] = None,
             parse_mode: str = "HTML") -> None:
    """Send a plain text message, splitting if > 4000 chars."""
    # Telegram max message length is 4096 chars
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    for i, chunk in enumerate(chunks):
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
        }
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        _tg(token, "sendMessage", **payload)


def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    _tg(token, "answerCallbackQuery",
        callback_query_id=callback_query_id, text=text, show_alert=False)


def edit_msg(token: str, chat_id: int, message_id: int, text: str,
             reply_markup: Optional[Dict] = None) -> None:
    _tg(token, "editMessageText",
        chat_id=chat_id, message_id=message_id,
        text=text, parse_mode="HTML",
        reply_markup=reply_markup)


def inline_kb(rows: List[List[Dict]]) -> Dict:
    """Build an inline keyboard from a list of button rows."""
    return {"inline_keyboard": rows}


def btn(text: str, data: str) -> Dict:
    """Create a callback_data button."""
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> Dict:
    return {"text": text, "url": url}


# ---------------------------------------------------------------------------
# Local API helpers
# ---------------------------------------------------------------------------

def _api(base_url: str, api_key: str, method: str,
         path: str, body: Optional[Dict] = None) -> Optional[Dict]:
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=30)
        else:
            r = requests.post(url, headers=headers, json=body or {}, timeout=60)
        if r.ok:
            return r.json()
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Menu builders
# ---------------------------------------------------------------------------

def main_menu_kb() -> Dict:
    return inline_kb([
        [btn("💬 محادثة AI", "menu:chat"),    btn("🤖 الوكلاء", "menu:agents")],
        [btn("🔄 المزودون", "menu:providers"), btn("🧠 النماذج", "menu:models")],
        [btn("📚 قاعدة المعرفة", "menu:rag"),  btn("📁 الملفات", "menu:files")],
        [btn("📊 الإحصائيات", "menu:stats"),   btn("⚙️ الإعدادات", "menu:settings")],
        [btn("📜 سجل المحادثات", "menu:history")],
    ])


def back_kb(target: str = "menu:main") -> Dict:
    return inline_kb([[btn("↩️ رجوع", target)]])


def back_and_refresh_kb(target: str, refresh: str) -> Dict:
    return inline_kb([[btn("🔄 تحديث", refresh), btn("↩️ رجوع", target)]])


# ---------------------------------------------------------------------------
# Screen renderers
# ---------------------------------------------------------------------------

def render_main_menu(user_name: str = "") -> str:
    greeting = f"مرحباً <b>{user_name}</b> 👋\n\n" if user_name else ""
    return (
        f"{greeting}"
        "🚀 <b>Y.S Agent System</b>\n"
        "منصة الذكاء الاصطناعي المتقدمة\n\n"
        "اختر من القائمة أدناه:"
    )


def render_providers(base_url: str, api_key: str) -> str:
    res = _api(base_url, api_key, "GET", "/api/providers")
    if not res or not res.get("ok"):
        return "❌ تعذّر تحميل المزودين."
    providers = res.get("providers", [])
    active = res.get("active", "")
    lines = ["🔌 <b>مزودو الذكاء الاصطناعي</b>\n"]
    for p in providers:
        status = "✅" if p.get("enabled") else "⛔"
        is_active = " 🌟" if p.get("name") == active else ""
        keys = p.get("key_count", 0)
        lines.append(
            f"{status} <b>{p['name']}</b>{is_active}\n"
            f"   النموذج الافتراضي: {p.get('default_model','—')} • مفاتيح: {keys}"
        )
    return "\n".join(lines)


def render_models(base_url: str, api_key: str, provider: str = "") -> str:
    path = f"/api/models?provider={provider}" if provider else "/api/models"
    res = _api(base_url, api_key, "GET", path)
    if not res or not res.get("ok"):
        return "❌ تعذّر تحميل النماذج."
    models = res.get("models", [])
    active = res.get("active", "")
    lines = [f"🧠 <b>النماذج المتاحة</b> ({provider or 'الحالي'})\n"]
    for m in models[:20]:  # limit to avoid huge messages
        mark = " ✅" if m == active else ""
        lines.append(f"• <code>{m}</code>{mark}")
    if not models:
        lines.append("لا توجد نماذج متاحة.")
    return "\n".join(lines)


def render_agents(base_url: str, api_key: str) -> tuple:
    """Returns (text, agents_list)"""
    res = _api(base_url, api_key, "GET", "/api/agents")
    if not res or not res.get("ok"):
        return "❌ تعذّر تحميل الوكلاء.", []
    agents = res.get("agents", [])
    lines = ["🤖 <b>الوكلاء المتاحون</b>\n"]
    for a in agents:
        tools_str = "، ".join(a.get("tools", [])) or "—"
        lines.append(
            f"<b>{a.get('label', a['name'])}</b> (<code>{a['name']}</code>)\n"
            f"   {a.get('role','')}\n"
            f"   🔧 الأدوات: {tools_str}"
        )
    return "\n".join(lines), agents


def render_agent_kb(agents: List[Dict]) -> Dict:
    rows = []
    row: List[Dict] = []
    for i, a in enumerate(agents):
        row.append(btn(a.get("label", a["name"]), f"run_agent:{a['name']}"))
        if len(row) == 2 or i == len(agents) - 1:
            rows.append(row)
            row = []
    rows.append([btn("↩️ رجوع", "menu:main")])
    return inline_kb(rows)


def render_stats(base_url: str, api_key: str) -> str:
    stats = _api(base_url, api_key, "GET", "/api/admin/stats")
    usage = _api(base_url, api_key, "GET", "/api/admin/usage")
    lines = ["📊 <b>إحصائيات النظام</b>\n"]
    if stats and stats.get("ok"):
        s = stats.get("stats", stats)
        lines.append(f"💬 طلبات الدردشة: {s.get('chat_requests',0)}")
        lines.append(f"❌ الأخطاء: {s.get('errors',0)}")
        lines.append(f"🔢 إجمالي التوكنات: {s.get('total_tokens',0)}")
    if usage and usage.get("ok"):
        prov_usage = usage.get("provider_usage", {})
        if prov_usage:
            lines.append("\n📈 <b>استخدام المزودين:</b>")
            for prov, count in list(prov_usage.items())[:5]:
                lines.append(f"  • {prov}: {count}")
    return "\n".join(lines) if len(lines) > 1 else "❌ تعذّر تحميل الإحصائيات."


def render_files(base_url: str, api_key: str, path: str = "") -> tuple:
    """Returns (text, items)"""
    url_path = f"/api/files/list?path={requests.utils.quote(path)}" if path else "/api/files/list"
    res = _api(base_url, api_key, "GET", url_path)
    if not res or not res.get("ok"):
        return "❌ تعذّر تحميل الملفات.", []
    items = res.get("items", [])
    lines = [f"📁 <b>مستعرض الملفات</b>{' — ' + path if path else ''}\n"]
    if not items:
        lines.append("📂 المجلد فارغ.")
    for item in items[:20]:
        icon = "📁" if item.get("type") == "dir" else "📄"
        size = f" ({item.get('size',0)} بايت)" if item.get("type") != "dir" else ""
        lines.append(f"{icon} {item.get('name','')}{size}")
    return "\n".join(lines), items


def render_rag_menu() -> str:
    return (
        "📚 <b>قاعدة المعرفة (RAG)</b>\n\n"
        "يمكنك إضافة نصوص إلى قاعدة المعرفة أو البحث فيها.\n\n"
        "📝 أرسل نصاً وسيتم إضافته تلقائياً، أو اختر من القائمة:"
    )


def render_history_kb(base_url: str, api_key: str) -> tuple:
    """Returns (text, kb)"""
    res = _api(base_url, api_key, "GET", "/api/conversations")
    if not res or not res.get("ok"):
        return "❌ تعذّر تحميل السجل.", back_kb()
    convs = res.get("conversations", [])
    if not convs:
        return "📜 لا توجد محادثات محفوظة.", back_kb()
    lines = ["📜 <b>سجل المحادثات</b>\n"]
    rows = []
    for i, c in enumerate(convs[:10]):
        lines.append(f"{i+1}. {c.get('title','محادثة')} ({c.get('messages',0)} رسائل)")
        rows.append([btn(f"🗂 {c.get('title','محادثة')[:25]}", f"conv:{c['id']}")])
    rows.append([btn("↩️ رجوع", "menu:main")])
    return "\n".join(lines), inline_kb(rows)


# ---------------------------------------------------------------------------
# Chat via API
# ---------------------------------------------------------------------------

def call_chat(base_url: str, api_key: str, message: str,
              provider: str = "", model: str = "") -> str:
    body: Dict[str, Any] = {"message": message}
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model
    res = _api(base_url, api_key, "POST", "/api/chat", body)
    if res and res.get("ok"):
        reply = res.get("reply", "")
        used = res.get("provider", "")
        return f"{reply}\n\n<i>🔌 {used}</i>" if used else reply
    return "❌ " + (res.get("error", "فشل الطلب") if res else "تعذّر الاتصال بالخادم.")


def run_agent_call(base_url: str, api_key: str, agent_name: str, message: str) -> str:
    res = _api(base_url, api_key, "POST", f"/api/agents/{agent_name}/run", {"message": message})
    if not res or not res.get("ok"):
        return "❌ " + (res.get("error", "فشل تشغيل الوكيل") if res else "خطأ")
    job_id = res.get("job_id", "")
    if not job_id:
        return "❌ لم يُنشأ معرّف المهمة."
    # Poll for result (up to 60s)
    for _ in range(60):
        time.sleep(1)
        job = _api(base_url, api_key, "GET", f"/api/jobs/{job_id}")
        if job and job.get("ok"):
            status = job.get("job", {}).get("status", "")
            if status == "completed":
                output = job.get("job", {}).get("output", "")
                return output or "✅ اكتملت المهمة."
            elif status == "error":
                return "❌ " + job.get("job", {}).get("output", "خطأ في الوكيل")
    return "⏱️ انتهت مهلة الانتظار. قد تكون المهمة لا تزال تعمل."


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------

class UserState:
    __slots__ = ("mode", "pending_data", "provider", "model", "rag_mode")

    def __init__(self):
        self.mode: str = "idle"          # idle | chatting | awaiting_rag | awaiting_search
        self.pending_data: str = ""      # agent name when awaiting chat for agent
        self.provider: str = ""
        self.model: str = ""
        self.rag_mode: str = "chat"      # chat | ingest | search


_user_states: Dict[int, UserState] = {}


def get_state(user_id: int) -> UserState:
    if user_id not in _user_states:
        _user_states[user_id] = UserState()
    return _user_states[user_id]


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

def run_bot(token: str, api_key: str, base_url: str = "http://127.0.0.1:8080"):
    """
    Run the full Arabic Telegram bot with inline keyboard menus.
    Supports: chat, agents, providers, models, files, RAG, stats, history, settings.
    """
    last_update = 0
    log.info("Telegram bot started (base_url=%s)", base_url)

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
                # Dispatch in a thread so blocking calls don't stall the loop
                t = threading.Thread(
                    target=_handle_update,
                    args=(token, api_key, base_url, item),
                    daemon=True,
                )
                t.start()

        except Exception as exc:
            log.exception("Telegram polling error: %s", exc)
            time.sleep(5)


def _handle_update(token: str, api_key: str, base_url: str, item: Dict):
    """Handle a single Telegram update (message or callback_query)."""
    try:
        cb = item.get("callback_query")
        if cb:
            _handle_callback(token, api_key, base_url, cb)
            return

        msg = item.get("message") or item.get("edited_message")
        if msg:
            _handle_message(token, api_key, base_url, msg)
    except Exception as exc:
        log.exception("Update handler error: %s", exc)


def _handle_callback(token: str, api_key: str, base_url: str, cb: Dict):
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    user = cb.get("from", {})
    user_id = user.get("id", chat_id)
    state = get_state(user_id)

    answer_callback(token, cb["id"])

    if data == "menu:main" or data == "start":
        name = user.get("first_name", "")
        edit_msg(token, chat_id, msg_id,
                 render_main_menu(name), main_menu_kb())

    elif data == "menu:chat":
        state.mode = "chatting"
        state.pending_data = ""
        edit_msg(token, chat_id, msg_id,
                 "💬 <b>وضع المحادثة</b>\n\nأرسل رسالتك وسأرد عليها باستخدام الذكاء الاصطناعي.\n\nاضغط /start للعودة إلى القائمة.",
                 back_kb("menu:main"))

    elif data == "menu:agents":
        text, agents = render_agents(base_url, api_key)
        kb = render_agent_kb(agents)
        edit_msg(token, chat_id, msg_id, text, kb)

    elif data.startswith("run_agent:"):
        agent_name = data.split(":", 1)[1]
        state.mode = "agent"
        state.pending_data = agent_name
        agent_label = agent_name
        # try to get the label
        res = _api(base_url, api_key, "GET", "/api/agents")
        if res and res.get("ok"):
            for a in res.get("agents", []):
                if a["name"] == agent_name:
                    agent_label = a.get("label", agent_name)
                    break
        edit_msg(token, chat_id, msg_id,
                 f"🤖 <b>وكيل: {agent_label}</b>\n\nأرسل مهمتك للوكيل:",
                 back_kb("menu:agents"))

    elif data == "menu:providers":
        text = render_providers(base_url, api_key)
        # Build provider switch buttons
        res = _api(base_url, api_key, "GET", "/api/providers")
        rows = []
        if res and res.get("ok"):
            for p in res.get("providers", []):
                if p.get("enabled"):
                    rows.append([btn(f"🔄 تفعيل {p['name']}", f"switch_provider:{p['name']}")])
        rows.append([btn("↩️ رجوع", "menu:main")])
        edit_msg(token, chat_id, msg_id, text, inline_kb(rows))

    elif data.startswith("switch_provider:"):
        pname = data.split(":", 1)[1]
        res = _api(base_url, api_key, "POST", "/api/providers/switch", {"provider": pname})
        if res and res.get("ok"):
            state.provider = pname
            answer_callback(token, cb["id"], f"✅ تم التبديل إلى {pname}")
        text = render_providers(base_url, api_key)
        res2 = _api(base_url, api_key, "GET", "/api/providers")
        rows = []
        if res2 and res2.get("ok"):
            for p in res2.get("providers", []):
                if p.get("enabled"):
                    rows.append([btn(f"🔄 تفعيل {p['name']}", f"switch_provider:{p['name']}")])
        rows.append([btn("↩️ رجوع", "menu:main")])
        edit_msg(token, chat_id, msg_id, text, inline_kb(rows))

    elif data == "menu:models":
        text = render_models(base_url, api_key, state.provider)
        res = _api(base_url, api_key, "GET", "/api/models")
        rows = []
        if res and res.get("ok"):
            for m in (res.get("models") or [])[:8]:
                rows.append([btn(f"✅ {m}", f"switch_model:{m}")])
        rows.append([btn("↩️ رجوع", "menu:main")])
        edit_msg(token, chat_id, msg_id, text, inline_kb(rows))

    elif data.startswith("switch_model:"):
        mname = data.split(":", 1)[1]
        res = _api(base_url, api_key, "POST", "/api/models/switch", {"model": mname})
        if res and res.get("ok"):
            state.model = mname
        text = render_models(base_url, api_key, state.provider)
        res2 = _api(base_url, api_key, "GET", "/api/models")
        rows = []
        if res2 and res2.get("ok"):
            for m in (res2.get("models") or [])[:8]:
                rows.append([btn(f"✅ {m}", f"switch_model:{m}")])
        rows.append([btn("↩️ رجوع", "menu:main")])
        edit_msg(token, chat_id, msg_id, text, inline_kb(rows))

    elif data == "menu:rag":
        state.mode = "rag"
        state.rag_mode = "chat"
        edit_msg(token, chat_id, msg_id,
                 render_rag_menu(),
                 inline_kb([
                     [btn("➕ إضافة نص", "rag:ingest"),  btn("🔍 بحث", "rag:search")],
                     [btn("🗑 مسح القاعدة", "rag:clear"), btn("↩️ رجوع", "menu:main")],
                 ]))

    elif data == "rag:ingest":
        state.mode = "rag"
        state.rag_mode = "ingest"
        edit_msg(token, chat_id, msg_id,
                 "📝 <b>إضافة إلى قاعدة المعرفة</b>\n\nأرسل النص الذي تريد إضافته:",
                 back_kb("menu:rag"))

    elif data == "rag:search":
        state.mode = "rag"
        state.rag_mode = "search"
        edit_msg(token, chat_id, msg_id,
                 "🔍 <b>البحث في قاعدة المعرفة</b>\n\nأرسل استعلام البحث:",
                 back_kb("menu:rag"))

    elif data == "rag:clear":
        res = _api(base_url, api_key, "POST", "/api/rag/clear", {})
        msg_text = "✅ تم مسح قاعدة المعرفة." if (res and res.get("ok")) else "❌ فشل المسح."
        edit_msg(token, chat_id, msg_id, msg_text,
                 inline_kb([[btn("↩️ رجوع", "menu:rag")]]))

    elif data == "menu:files":
        text, items = render_files(base_url, api_key)
        rows = []
        for item in items[:8]:
            if item.get("type") == "dir":
                rows.append([btn(f"📁 {item['name']}", f"file_dir:{item['name']}")])
            else:
                rows.append([btn(f"📄 {item['name']}", f"file_read:{item['name']}")])
        rows.append([btn("↩️ رجوع", "menu:main")])
        edit_msg(token, chat_id, msg_id, text, inline_kb(rows))

    elif data.startswith("file_read:"):
        fname = data.split(":", 1)[1]
        res = _api(base_url, api_key, "GET",
                   f"/api/files/read?path={requests.utils.quote(fname)}")
        if res and res.get("ok"):
            content = res.get("content", "")[:2000]
            send_msg(token, chat_id,
                     f"📄 <b>{fname}</b>\n\n<pre>{content}</pre>",
                     back_kb("menu:files"))
        else:
            send_msg(token, chat_id, "❌ تعذّر قراءة الملف.", back_kb("menu:files"))

    elif data == "menu:stats":
        text = render_stats(base_url, api_key)
        edit_msg(token, chat_id, msg_id, text,
                 back_and_refresh_kb("menu:main", "menu:stats"))

    elif data == "menu:history":
        text, kb = render_history_kb(base_url, api_key)
        edit_msg(token, chat_id, msg_id, text, kb)

    elif data.startswith("conv:"):
        cid = data.split(":", 1)[1]
        res = _api(base_url, api_key, "GET", f"/api/conversation/{cid}")
        if res and res.get("ok"):
            msgs = res.get("conversation", {}).get("messages", [])
            lines = [f"🗂 <b>المحادثة</b>\n"]
            for m in msgs[-5:]:  # last 5 messages
                role_ar = "👤 أنت" if m.get("role") == "user" else "🤖 الذكاء الاصطناعي"
                content = str(m.get("content", ""))[:300]
                lines.append(f"<b>{role_ar}:</b> {content}")
            send_msg(token, chat_id, "\n\n".join(lines), back_kb("menu:history"))
        else:
            send_msg(token, chat_id, "❌ تعذّر تحميل المحادثة.", back_kb("menu:history"))

    elif data == "menu:settings":
        res = _api(base_url, api_key, "GET", "/api/providers")
        active_p = res.get("active", "—") if res else "—"
        res2 = _api(base_url, api_key, "GET", "/api/models")
        active_m = res2.get("active", "—") if res2 else "—"
        text = (
            "⚙️ <b>الإعدادات</b>\n\n"
            f"🔌 المزود الحالي: <code>{active_p}</code>\n"
            f"🧠 النموذج الحالي: <code>{active_m or 'الافتراضي'}</code>\n\n"
            "استخدم الأزرار للتغيير:"
        )
        edit_msg(token, chat_id, msg_id, text,
                 inline_kb([
                     [btn("🔄 تغيير المزود", "menu:providers"),
                      btn("🧠 تغيير النموذج", "menu:models")],
                     [btn("↩️ رجوع", "menu:main")],
                 ]))


def _handle_message(token: str, api_key: str, base_url: str, msg: Dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    user = msg.get("from", {})
    user_id = user.get("id", chat_id)
    user_name = user.get("first_name", "")
    state = get_state(user_id)

    if not text:
        return

    # Commands
    if text.startswith("/start") or text == "/قائمة":
        state.mode = "idle"
        send_msg(token, chat_id, render_main_menu(user_name), main_menu_kb())
        return

    if text.startswith("/help") or text == "/مساعدة":
        help_text = (
            "📖 <b>دليل الاستخدام</b>\n\n"
            "/start — القائمة الرئيسية\n"
            "/chat نص — محادثة مباشرة مع الذكاء الاصطناعي\n"
            "/providers — قائمة المزودين\n"
            "/models — قائمة النماذج\n"
            "/agents — قائمة الوكلاء\n"
            "/stats — إحصائيات النظام\n"
            "/rag نص — إضافة نص لقاعدة المعرفة\n"
            "/search استعلام — بحث في قاعدة المعرفة\n"
            "/files — استعراض الملفات\n\n"
            "أو اختر من القائمة التفاعلية أعلاه 👆"
        )
        send_msg(token, chat_id, help_text, back_kb("start"))
        return

    if text.startswith("/chat "):
        prompt = text[6:].strip()
        send_msg(token, chat_id, "⏳ جاري المعالجة…")
        reply = call_chat(base_url, api_key, prompt, state.provider, state.model)
        send_msg(token, chat_id, reply, back_kb("start"))
        return

    if text.startswith("/providers"):
        send_msg(token, chat_id, render_providers(base_url, api_key), back_kb("start"))
        return

    if text.startswith("/models"):
        send_msg(token, chat_id, render_models(base_url, api_key), back_kb("start"))
        return

    if text.startswith("/agents"):
        agents_text, agents = render_agents(base_url, api_key)
        send_msg(token, chat_id, agents_text, render_agent_kb(agents))
        return

    if text.startswith("/stats"):
        send_msg(token, chat_id, render_stats(base_url, api_key), back_kb("start"))
        return

    if text.startswith("/files"):
        files_text, items = render_files(base_url, api_key)
        rows = [[btn(f"{'📁' if i.get('type')=='dir' else '📄'} {i['name']}", f"file_read:{i['name']}")] for i in items[:8]]
        rows.append([btn("↩️ رجوع", "start")])
        send_msg(token, chat_id, files_text, inline_kb(rows))
        return

    if text.startswith("/rag "):
        rag_text = text[5:].strip()
        res = _api(base_url, api_key, "POST", "/api/rag/ingest", {"text": rag_text, "source": "telegram"})
        if res and res.get("ok"):
            send_msg(token, chat_id, "✅ تمت الإضافة إلى قاعدة المعرفة.", back_kb("start"))
        else:
            send_msg(token, chat_id, "❌ فشلت الإضافة.", back_kb("start"))
        return

    if text.startswith("/search "):
        query = text[8:].strip()
        res = _api(base_url, api_key, "POST", "/api/rag/search", {"query": query})
        if res and res.get("ok"):
            results = res.get("results", [])
            if results:
                lines = [f"🔍 <b>نتائج البحث عن:</b> {query}\n"]
                for r in results[:5]:
                    lines.append(f"📌 {r.get('text','')[:200]}")
                send_msg(token, chat_id, "\n\n".join(lines), back_kb("start"))
            else:
                send_msg(token, chat_id, "🔍 لا توجد نتائج.", back_kb("start"))
        else:
            send_msg(token, chat_id, "❌ فشل البحث.", back_kb("start"))
        return

    # Context-based input handling
    if state.mode == "chatting":
        send_msg(token, chat_id, "⏳ جاري المعالجة…")
        reply = call_chat(base_url, api_key, text, state.provider, state.model)
        send_msg(token, chat_id, reply,
                 inline_kb([[btn("💬 رسالة أخرى", "menu:chat"),
                              btn("↩️ القائمة", "menu:main")]]))
        return

    if state.mode == "agent" and state.pending_data:
        agent_name = state.pending_data
        send_msg(token, chat_id, f"⏳ يعمل الوكيل <b>{agent_name}</b>…")
        result = run_agent_call(base_url, api_key, agent_name, text)
        state.mode = "idle"
        state.pending_data = ""
        send_msg(token, chat_id, result[:3800],
                 inline_kb([[btn("🤖 وكيل آخر", "menu:agents"),
                              btn("↩️ القائمة", "menu:main")]]))
        return

    if state.mode == "rag":
        if state.rag_mode == "ingest":
            res = _api(base_url, api_key, "POST", "/api/rag/ingest",
                       {"text": text, "source": "telegram"})
            if res and res.get("ok"):
                send_msg(token, chat_id, "✅ تمت الإضافة بنجاح.",
                         inline_kb([[btn("➕ إضافة المزيد", "rag:ingest"),
                                     btn("↩️ رجوع", "menu:rag")]]))
            else:
                send_msg(token, chat_id, "❌ فشلت الإضافة.", back_kb("menu:rag"))
        elif state.rag_mode == "search":
            res = _api(base_url, api_key, "POST", "/api/rag/search", {"query": text})
            if res and res.get("ok"):
                results = res.get("results", [])
                if results:
                    lines = [f"🔍 <b>نتائج:</b> {text}\n"]
                    for r in results[:5]:
                        lines.append(f"📌 {r.get('text','')[:300]}")
                    send_msg(token, chat_id, "\n\n".join(lines),
                             inline_kb([[btn("🔍 بحث آخر", "rag:search"),
                                         btn("↩️ رجوع", "menu:rag")]]))
                else:
                    send_msg(token, chat_id, "🔍 لا توجد نتائج.",
                             back_kb("menu:rag"))
            else:
                send_msg(token, chat_id, "❌ فشل البحث.", back_kb("menu:rag"))
        state.mode = "idle"
        state.rag_mode = "chat"
        return

    # Default: treat as chat
    send_msg(token, chat_id, "⏳ جاري المعالجة…")
    reply = call_chat(base_url, api_key, text, state.provider, state.model)
    send_msg(token, chat_id, reply,
             inline_kb([[btn("💬 رسالة أخرى", "menu:chat"),
                         btn("↩️ القائمة", "menu:main")]]))
