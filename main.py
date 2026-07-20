"""
main.py

Unified AI Gateway (FastAPI) + CrewAI agent builder.

Changes in this updated version (per your requirements):
1. Reads PORT dynamically from environment using os.environ.get("PORT", 8000) and binds to 0.0.0.0.
2. Agents' LLM configuration uses a ChatOpenAI-style config where `base_url` points to the gateway
   on the dynamic port with the `/v1` prefix.
3. Agents are configured to pass `LITELLM_MASTER_KEY` as `api_key` to authenticate to the local gateway.
4. The gateway validates incoming calls (from agents or clients) using the LITELLM_MASTER_KEY header
   (X-API-Key or Authorization Bearer) when that env var is set.

Notes:
- This file uses the same provider routing logic as before (LiteLLM preferred, provider fallbacks).
- `ChatOpenAI` here is used as a configuration descriptor (many agent-builder tools accept a dict
  describing an OpenAI-compatible LLM). Adapt the exact CrewAI/AgentBuilder call if your CrewAI SDK
  expects a different shape.
- Ensure the environment variables below are set in Railway (list provided separately).
"""

import os
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Try import LiteLLM (if available)
try:
    import litellm
    from litellm import Client as LiteLLMClient  # interface may differ by version
    LITELLM_AVAILABLE = True
except Exception:
    LiteLLMClient = None
    LITELLM_AVAILABLE = False

# Try import CrewAI (optional integration)
try:
    import crewai
    from crewai import Agent as CrewAgent  # hypothetical API (adapt if necessary)
    from crewai_tools import AgentBuilder   # hypothetical helper (adapt if necessary)
    CREWAI_AVAILABLE = True
except Exception:
    crewai = None
    CrewAgent = None
    AgentBuilder = None
    CREWAI_AVAILABLE = False

# -----------------------
# Configuration (ENV) - dynamic PORT handling
# -----------------------
# Railway sets PORT automatically; default to 8000 for local dev.
PORT = int(os.environ.get("PORT", 8000))
# The server should always listen on 0.0.0.0 to accept external connections in Railway.
HOST = os.environ.get("HOST", "0.0.0.0")

# Gateway / provider keys
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_BASE = os.environ.get("GROQ_API_BASE", "https://api.groq.com/openai/v1")

# Master key used by local agents (and optionally other trusted clients) to authenticate to the gateway.
# Agents will pass this as their api_key when configured to use the local gateway.
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# Local base URL used by agents. If running agents in the same container, 127.0.0.1 is appropriate.
# In containerized deployments where agents run in separate services, set LOCAL_BASE_URL in Railway to the public URL.
LOCAL_BASE = os.environ.get("LOCAL_BASE_URL", f"http://127.0.0.1:{PORT}")

# We expose the OpenAI-compatible path under /v1
OPENAI_COMPAT_PREFIX = "/v1"
OPENAI_CHAT_PATH = f"{OPENAI_COMPAT_PREFIX}/chat/completions"

# Logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("unified-gateway")

# FastAPI app
app = FastAPI(title="Unified AI Gateway + CrewAI (updated)", version="1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# -----------------------
# Pydantic models (OpenAI-compatible subset)
# -----------------------
class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.0
    stream: Optional[bool] = False
    provider: Optional[str] = None
    options: Optional[Dict[str, Any]] = {}


# -----------------------
# Authentication helper (validates LITELLM_MASTER_KEY if configured)
# -----------------------
def _auth_header_matches_master(x_api_key: Optional[str], authorization: Optional[str]) -> bool:
    """
    Accept either:
      - X-API-Key: <LITELLM_MASTER_KEY>
      - Authorization: Bearer <LITELLM_MASTER_KEY>
    If LITELLM_MASTER_KEY is empty (not configured), treat gateway as public (no check).
    """
    if not LITELLM_MASTER_KEY:
        return True
    if x_api_key and x_api_key == LITELLM_MASTER_KEY:
        return True
    if authorization:
        # Authorization: Bearer <token>
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == LITELLM_MASTER_KEY:
            return True
    return False


def require_gateway_key(x_api_key: Optional[str], authorization: Optional[str]):
    if not _auth_header_matches_master(x_api_key, authorization):
        raise HTTPException(status_code=401, detail="Missing or invalid gateway API key (LITELLM_MASTER_KEY).")


# -----------------------
# Provider resolution helpers
# -----------------------
def resolve_provider(model: Optional[str], explicit_provider: Optional[str] = None):
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


# -----------------------
# Unified provider generation with retries
# -----------------------
class ProviderError(Exception):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
       retry=retry_if_exception_type((httpx.HTTPError, ProviderError)))
def provider_generate(provider: Optional[str], api_key: Optional[str], model: str, prompt: str,
                      max_tokens: int = 512, temperature: float = 0.0, options: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Prefer LiteLLM if available; otherwise use provider-specific HTTP fallbacks.
    """
    options = options or {}

    # 1) LiteLLM
    if LITELLM_AVAILABLE and LiteLLMClient is not None:
        try:
            client = LiteLLMClient()  # may use env-based config internally
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
            log.exception("LiteLLM error for %s: %s", model, exc)
            if is_rate_limit_error(exc):
                raise ProviderError("Rate-limited or transient error: " + str(exc))
            # fall through to fallback providers

    # 2) Gemini fallback
    if provider == "gemini":
        if not GEMINI_API_KEY:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateText"
            headers = {"Content-Type": "application/json"}
            payload = {"prompt": {"text": prompt}, "temperature": temperature, "maxOutputTokens": int(max_tokens)}
            params = {"key": GEMINI_API_KEY}
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

    # 3) Groq fallback
    if provider == "groq":
        if not GROQ_API_KEY:
            raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")
        try:
            url = f"{GROQ_API_BASE}/chat/completions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(max_tokens),
                "temperature": float(temperature)
            }
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

    # No provider available
    raise HTTPException(status_code=500, detail="No provider available and LiteLLM fallback failed.")


# -----------------------
# OpenAI-compatible endpoint: used by local agents via ChatOpenAI-style base_url
# -----------------------
@app.post(OPENAI_CHAT_PATH)
async def openai_chat_endpoint(req: ChatRequest, x_api_key: Optional[str] = Header(None), authorization: Optional[str] = Header(None)):
    """
    Minimal OpenAI-compatible Chat Completions endpoint.
    Requires LITELLM_MASTER_KEY if configured (via X-API-Key or Authorization Bearer).
    """
    # authenticate client (agents should send LITELLM_MASTER_KEY)
    require_gateway_key = True  # we will use our auth helper below
    if LITELLM_MASTER_KEY:
        if not _auth_header_matches_master(x_api_key, authorization):
            raise HTTPException(status_code=401, detail="Missing or invalid LITELLM_MASTER_KEY (X-API-Key or Authorization Bearer required)")

    if not req.messages:
        raise HTTPException(status_code=400, detail="messages are required")

    model = req.model or "google/gemini-1.5-flash"
    provider, api_key = resolve_provider(model, req.provider)

    # Build a simple prompt from messages (concatenate)
    parts = []
    for m in req.messages:
        r = m.get("role", "user")
        c = m.get("content", "")
        parts.append(f"[{r}] {c}")
    prompt = "\n".join(parts)

    try:
        # For gateway-originated requests (from agents), we will pass the provider api keys (if needed).
        res = provider_generate(provider, api_key, model, prompt,
                                max_tokens=req.max_tokens or 512,
                                temperature=req.temperature or 0.0,
                                options=req.options or {})
        text = res.get("text", "")
        cid = f"chatcmpl-{uuid.uuid4().hex}"
        choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        out = {
            "id": cid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [choice],
            "usage": {"prompt_tokens": 0, "completion_tokens": max(1, len(text) // 4), "total_tokens": max(1, len(text) // 4)}
        }
        return out
    except ProviderError as exc:
        log.exception("Transient provider error: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Unexpected error in openai_chat_endpoint: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error")


# -----------------------
# Health endpoints
# -----------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "unified-ai-gateway", "time": int(time.time())}

@app.get("/ready")
def ready():
    return {"ok": True, "providers": {"gemini": bool(GEMINI_API_KEY), "groq": bool(GROQ_API_KEY), "litellm": LITELLM_AVAILABLE}}

@app.get("/live")
def live():
    return {"ok": True, "time": int(time.time())}


# -----------------------
# CrewAI Agent builder (ChatOpenAI-style config pointing to local gateway)
# -----------------------
def create_crewai_agent(name: str, model: str, agent_options: Optional[Dict] = None):
    """
    Build a CrewAI agent configured to use the local gateway.
    The agent's LLM config uses a ChatOpenAI-style descriptor with base_url pointing to:
      - LOCAL_BASE (public URL or 127.0.0.1) + /v1
    Agents will pass LITELLM_MASTER_KEY as their api_key when calling the gateway.
    """
    agent_options = agent_options or {}
    # The ChatOpenAI-config style; adapt shape if CrewAI expects a different dict.
    chat_openai_cfg = {
        "type": "ChatOpenAI",
        "base_url": f"{LOCAL_BASE}{OPENAI_COMPAT_PREFIX}",  # e.g. http://127.0.0.1:8000/v1
        "model": model,
        "api_key": LITELLM_MASTER_KEY,   # agents send the master key to authenticate
        # additional options passed through
    }

    log.info("Creating CrewAI agent '%s' with ChatOpenAI base_url=%s", name, chat_openai_cfg["base_url"])

    # Prefer the AgentBuilder if available (hypothetical API)
    if CREWAI_AVAILABLE and AgentBuilder is not None:
        try:
            builder = AgentBuilder(name=name, llm_config=chat_openai_cfg, **(agent_options or {}))
            agent = builder.build()
            log.info("Agent '%s' built via AgentBuilder", name)
            return agent
        except Exception as exc:
            log.exception("AgentBuilder failed for %s: %s", name, exc)

    # Fallback direct Agent instantiation (hypothetical)
    if CREWAI_AVAILABLE and CrewAgent is not None:
        try:
            agent = CrewAgent(name=name, llm_config=chat_openai_cfg, **(agent_options or {}))
            log.info("Agent '%s' created via CrewAgent", name)
            return agent
        except Exception as exc:
            log.exception("CrewAgent instantiation failed for %s: %s", name, exc)

    log.warning("CrewAI SDK not available in runtime; returning None for agent %s", name)
    return None


@app.get("/agents/bootstrap")
def bootstrap_agents():
    """
    Example bootstrap creating agents that use the local gateway as their LLM backend.
    Two examples:
      - data_agent -> google/gemini-1.5-flash (heavy processing)
      - fast_agent -> groq/llama3-70b-8192 (low latency)
    """
    agents = {
        "data_agent": create_crewai_agent("data_agent", "google/gemini-1.5-flash"),
        "fast_agent": create_crewai_agent("fast_agent", "groq/llama3-70b-8192")
    }
    return {"ok": True, "agents": {k: bool(v) for k, v in agents.items()}}


# -----------------------
# If run as script -> run uvicorn using dynamic PORT and HOST (0.0.0.0)
# -----------------------
if __name__ == "__main__":
    log.info("Unified AI Gateway starting (local mode). LiteLLM=%s CrewAI=%s", LITELLM_AVAILABLE, CREWAI_AVAILABLE)
    log.info("Listening on host=%s port=%s ; LOCAL_BASE=%s", HOST, PORT, LOCAL_BASE)
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, workers=int(os.environ.get("WEB_CONCURRENCY", "1")))
