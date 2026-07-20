"""
main.py

Unified AI Gateway (FastAPI) + CrewAI agent builder.

- يوفّر واجهة OpenAI-compatible على /v1/chat/completions
- يوجّه الطلبات إلى LiteLLM إن توافرت، وإلى Gemini/Groq كـ fallbacks
- يبني وكلاء CrewAI ليتّصلوا بالسيرفر المحلي بدل تضمين مفاتيح المزود
"""

import os
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# محاولة استيراد LiteLLM (إذا متوفر)
try:
    import litellm
    from litellm import Client as LiteLLMClient  # ملاحظة: الواجهة قد تختلف حسب الإصدار
    LITELLM_AVAILABLE = True
except Exception:
    LiteLLMClient = None
    LITELLM_AVAILABLE = False

# محاولة استيراد CrewAI (إن متوفر)
try:
    import crewai
    from crewai import Agent as CrewAgent  # افتراضي
    from crewai_tools import AgentBuilder  # افتراضي
    CREWAI_AVAILABLE = True
except Exception:
    crewai = None
    CrewAgent = None
    AgentBuilder = None
    CREWAI_AVAILABLE = False

# إعدادات من البيئة
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0")
LOCAL_BASE = os.environ.get("LOCAL_BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_COMPAT_PREFIX = "/v1"
OPENAI_CHAT_PATH = f"{OPENAI_COMPAT_PREFIX}/chat/completions"

# Logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("unified-gateway")

app = FastAPI(title="Unified AI Gateway + CrewAI", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ضيّق في الإنتاج
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------
# Models (OpenAI-compatible subset)
# ------------------------
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.0
    stream: Optional[bool] = False
    provider: Optional[str] = None
    options: Optional[Dict[str, Any]] = {}


# ------------------------
# Provider resolution helpers
# ------------------------
def resolve_provider(model: Optional[str], explicit_provider: Optional[str] = None):
    """
    يقرر المزود (gemini أو groq) اعتمادًا على اسم النموذج أو المزوّد الصريح.
    ويرجع (provider_key, api_key) أو (None, None) لترك القرار إلى LiteLLM.
    """
    if explicit_provider:
        p = explicit_provider.lower()
        if p in ("gemini", "google"):
            return "gemini", GEMINI_API_KEY
        if p == "groq":
            return "groq", GROQ_API_KEY
        return p, None

    if model:
        m = model.lower()
        if m.startswith("google/") or "gemini" in m:
            return "gemini", GEMINI_API_KEY
        if m.startswith("groq/") or "groq" in m:
            return "groq", GROQ_API_KEY

    return None, None


def is_rate_limit_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "rate limit" in s or "quota" in s or "429" in s


# ------------------------
# Unified generation logic with retries
# ------------------------
class ProviderError(Exception):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
       retry=retry_if_exception_type((httpx.HTTPError, ProviderError)))
def provider_generate(provider: Optional[str], api_key: Optional[str], model: str,
                      prompt: str, max_tokens: int = 512, temperature: float = 0.0,
                      options: Optional[Dict] = None) -> Dict[str, Any]:
    """
    محاولة استخدام LiteLLM أولاً، ثم fallback لمزود محدد (Gemini/Groq) عبر HTTP.
    يرفع ProviderError للخطأ المؤقت ليتولى tenacity إعادة المحاولة.
    """
    options = options or {}

    # 1) LiteLLM (إن وُجد)
    if LITELLM_AVAILABLE and LiteLLMClient is not None:
        try:
            client = LiteLLMClient()  # قد يقرأ client vars من البيئة
            # ملاحظة: واجهة litellm قد تختلف. عدِّل load_model/generate حسب الإصدار.
            lm = client.load_model(model, api_key=api_key, **options)
            out = lm.generate(prompt, max_tokens=max_tokens, temperature=temperature, **options)
            if isinstance(out, dict):
                text = out.get("text") or out.get("output") or str(out)
                meta = out.get("meta", {})
            else:
                text = str(out)
                meta = {}
            return {"text": text, "meta": meta, "provider": provider or "litellm"}
        except Exception as exc:
            log.exception("LiteLLM failure for %s: %s", model, exc)
            if is_rate_limit_error(exc):
                raise ProviderError("Rate limited: " + str(exc))
            # else نتابع لفالباك

    # 2) Gemini fallback (Google Generative API - شكل الاستجابة قد يختلف)
    if provider == "gemini":
        if not api_key:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateText"
            headers = {"Content-Type": "application/json"}
            payload = {"prompt": {"text": prompt}, "temperature": temperature, "maxOutputTokens": int(max_tokens)}
            params = {"key": api_key}
            with httpx.Client(timeout=60.0) as client:
                r = client.post(url, json=payload, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
                text = ""
                if "candidates" in data and len(data["candidates"]) > 0:
                    cand = data["candidates"][0]
                    text = cand.get("content") or cand.get("output", {}).get("content", "") or str(cand)
                else:
                    text = str(data)
                return {"text": text, "meta": data, "provider": "gemini"}
        except httpx.HTTPStatusError as exc:
            log.exception("Gemini HTTP error: %s", exc)
            if exc.response.status_code == 429 or is_rate_limit_error(exc):
                raise ProviderError("Gemini rate-limited")
            raise HTTPException(status_code=502, detail="Gemini provider error: " + str(exc))
        except Exception as exc:
            log.exception("Gemini fallback failed: %s", exc)
            raise HTTPException(status_code=502, detail="Gemini provider failure: " + str(exc))

    # 3) Groq fallback (OpenAI-compatible)
    if provider == "groq":
        if not api_key:
            raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")
        try:
            base = os.environ.get("GROQ_API_BASE", "https://api.groq.com/openai/v1")
            url = f"{base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": int(max_tokens), "temperature": float(temperature)}
            with httpx.Client(timeout=60.0) as client:
                r = client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                text = ""
                try:
                    text = data["choices"][0]["message"]["content"]
                except Exception:
                    text = str(data)
                return {"text": text, "meta": data, "provider": "groq"}
        except httpx.HTTPStatusError as exc:
            log.exception("Groq HTTP error: %s", exc)
            if exc.response.status_code == 429 or is_rate_limit_error(exc):
                raise ProviderError("Groq rate-limited")
            raise HTTPException(status_code=502, detail="Groq provider error: " + str(exc))
        except Exception as exc:
            log.exception("Groq fallback failed: %s", exc)
            raise HTTPException(status_code=502, detail="Groq provider failure: " + str(exc))

    raise HTTPException(status_code=500, detail="No provider available and LiteLLM fallback failed.")


# ------------------------
# OpenAI-compatible endpoint (/v1/chat/completions)
# ------------------------
@app.post(OPENAI_CHAT_PATH)
async def openai_chat(req: ChatRequest):
    if not req.messages or len(req.messages) == 0:
        raise HTTPException(status_code=400, detail="messages are required")

    model = req.model or "google/gemini-1.5-flash"
    provider, api_key = resolve_provider(model, req.provider)

    # نركّب النص من الرسائل ببساطة؛ يمكن تحسين ذلك لاحقًا
    parts = []
    for m in req.messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        parts.append(f"[{role}] {content}")
    prompt = "\n".join(parts)

    try:
        res = provider_generate(provider, api_key, model, prompt,
                                max_tokens=req.max_tokens or 512,
                                temperature=req.temperature or 0.0,
                                options=req.options or {})
        text = res.get("text", "")
        cid = f"chatcmpl-{uuid.uuid4().hex}"
        choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        resp = {
            "id": cid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [choice],
            "usage": {"prompt_tokens": 0, "completion_tokens": max(1, len(text) // 4), "total_tokens": max(1, len(text) // 4)}
        }
        return resp
    except ProviderError as exc:
        log.exception("Transient provider error: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Unexpected error in openai_chat: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error")


# ------------------------
# Health endpoints
# ------------------------
@app.get("/health")
def health():
    return {"ok": True, "time": int(time.time()), "service": "unified-ai-gateway"}


@app.get("/ready")
def ready():
    return {"ok": True, "providers": {"gemini": bool(GEMINI_API_KEY), "groq": bool(GROQ_API_KEY)}}


@app.get("/live")
def live():
    return {"ok": True, "time": int(time.time())}


# ------------------------
# CrewAI agent builder: agents تستخدم الواجهة المحلية كـ OpenAI-compatible LLM
# ------------------------
def create_crewai_agent(name: str, model: str, options: Optional[Dict] = None):
    """
    ينشئ وكيل CrewAI مهيأ لاستخدام الواجهة المحلية (LOCAL_BASE + OPENAI_CHAT_PATH).
    واجهات CrewAI قد تختلف؛ هذا مثال افتراضي قابل للتعديل.
    """
    llm_base = LOCAL_BASE + OPENAI_CHAT_PATH
    options = options or {}

    if CREWAI_AVAILABLE and AgentBuilder is not None:
        try:
            llm_cfg = {"type": "openai_compatible", "base_url": LOCAL_BASE, "model": model, "api_key": ""}
            builder = AgentBuilder(name=name, llm_config=llm_cfg, **options)
            agent = builder.build()
            log.info("Created CrewAI agent via AgentBuilder: %s -> %s", name, model)
            return agent
        except Exception as exc:
            log.exception("AgentBuilder failed, falling back: %s", exc)

    if CREWAI_AVAILABLE and CrewAgent is not None:
        try:
            agent = CrewAgent(name=name, llm_url=LOCAL_BASE, model=model, **(options or {}))
            log.info("Created CrewAgent: %s -> %s", name, model)
            return agent
        except Exception as exc:
            log.exception("CrewAgent instantiation failed: %s", exc)

    log.warning("CrewAI not available; returning None for agent %s", name)
    return None


@app.get("/agents/bootstrap")
def bootstrap_agents():
    agents = {
        "data_agent": create_crewai_agent("data_agent", "google/gemini-1.5-flash"),
        "fast_agent": create_crewai_agent("fast_agent", "groq/llama3-70b-8192"),
    }
    return {"ok": True, "agents": {k: bool(v) for k, v in agents.items()}}


# ------------------------
# Run locally (uvicorn) for smoke tests
# ------------------------
if __name__ == "__main__":
    log.info("Starting Unified AI Gateway on %s:%s", HOST, PORT)
    log.info("LiteLLM available: %s ; CrewAI available: %s", LITELLM_AVAILABLE, CREWAI_AVAILABLE)
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, workers=1)
