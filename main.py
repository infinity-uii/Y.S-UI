# https://github.com/infinity-uii/Y.S-UI/blob/main/main.py
"""
main.py

Unified AI Gateway (FastAPI) + Crew-style agent runner.

- Reads DATABASE_URL from environment (Railway Postgres).
- Configures agents' LLM endpoint to use the cloud gateway at https://railway.app (ChatOpenAI-compatible).
- Passes LITELLM_MASTER_KEY for agent -> gateway authentication.
- Builds two agents:
    1) Researcher Agent -> model "google/gemini-1.5-flash" (provider: gemini)
    2) Writer Agent -> model "llama-3.3-70b-versatile" (provider: groq)
- Creates a task for each agent, runs them in order, and stores outputs to the DB.
"""
from typing import Any, Dict, List, Optional
import os
import time
import uuid
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Optional SDK imports (non-fatal if not installed)
try:
    import crewai
    from crewai import Agent as CrewAgent  # hypothetical
    from crewai_tools import AgentBuilder
    CREWAI_AVAILABLE = True
except Exception:
    crewai = None
    CrewAgent = None
    AgentBuilder = None
    CREWAI_AVAILABLE = False

# SQLAlchemy for DB persistence
try:
    from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, Text, DateTime, JSON
    from sqlalchemy import insert
    from sqlalchemy.exc import SQLAlchemyError
    SQLALCHEMY_AVAILABLE = True
except Exception:
    SQLALCHEMY_AVAILABLE = False

# -----------------------
# Configuration (ENV)
# -----------------------
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

# Provider API keys (if you also want direct provider fallbacks)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_BASE = os.environ.get("GROQ_API_BASE", "https://api.groq.com/openai/v1")

# Master key for gateway auth (agents must send this)
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# DATABASE_URL (Railway Postgres) - REQUIREMENT #1
DATABASE_URL = os.environ.get("DATABASE_URL")  # expected: postgres://...
if not DATABASE_URL:
    # Fallback to local sqlite for dev if DATABASE_URL not set
    DATABASE_URL = os.environ.get("LOCAL_DATABASE_URL", "sqlite:///./local_dev.sqlite")
    # Note: For production on Railway, set DATABASE_URL env var.

# Gateway cloud base (per your request) - REQUIREMENT #2
# Agents will use this base and append /v1 paths like /v1/chat/completions
GATEWAY_CLOUD_BASE = "https://railway.app"

# OpenAI-compatible paths
OPENAI_COMPAT_PREFIX = "/v1"
OPENAI_CHAT_PATH = f"{OPENAI_COMPAT_PREFIX}/chat/completions"

# Logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("unified-gateway")

# FastAPI app
app = FastAPI(title="Unified AI Gateway + Crew Runner", version="1.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# -----------------------
# Simple DB setup (SQLAlchemy Core)
# -----------------------
engine = None
metadata = None
agent_outputs_table = None

if SQLALCHEMY_AVAILABLE:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        metadata = MetaData()
        # Table to store agent outputs
        agent_outputs_table = Table(
            "agent_outputs",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("agent_name", String(128), nullable=False),
            Column("task_name", String(256), nullable=False),
            Column("content", Text, nullable=False),
            Column("meta", JSON, nullable=True),
            Column("created_at", DateTime, default=datetime.utcnow, nullable=False),
        )
        metadata.create_all(engine)
        log.info("Database initialized and agent_outputs table ensured.")
    except Exception as exc:
        log.exception("Database initialization failed, continuing without persistence: %s", exc)
        engine = None
        agent_outputs_table = None
else:
    log.warning("SQLAlchemy not available; outputs will not be persisted to DATABASE_URL.")

def store_output(agent_name: str, task_name: str, content: str, meta: Optional[Dict] = None) -> Optional[int]:
    """Insert agent output into DB; returns row id or None if not persisted."""
    if engine is None or agent_outputs_table is None:
        log.info("DB not configured; skipping persist for %s/%s", agent_name, task_name)
        return None
    try:
        with engine.connect() as conn:
            stmt = insert(agent_outputs_table).values(
                agent_name=agent_name,
                task_name=task_name,
                content=content,
                meta=meta or {},
                created_at=datetime.utcnow()
            )
            res = conn.execute(stmt)
            conn.commit()
            inserted_id = res.inserted_primary_key[0] if res.inserted_primary_key else None
            log.info("Stored output id=%s for %s/%s", inserted_id, agent_name, task_name)
            return inserted_id
    except SQLAlchemyError as exc:
        log.exception("Failed to store output in DB: %s", exc)
        return None

# -----------------------
# Auth helpers for gateway
# -----------------------
def _auth_header_matches_master(x_api_key: Optional[str], authorization: Optional[str]) -> bool:
    if not LITELLM_MASTER_KEY:
        return True
    if x_api_key and x_api_key == LITELLM_MASTER_KEY:
        return True
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == LITELLM_MASTER_KEY:
            return True
    return False

def require_gateway_key(x_api_key: Optional[str], authorization: Optional[str]):
    if not _auth_header_matches_master(x_api_key, authorization):
        raise HTTPException(status_code=401, detail="Missing or invalid gateway API key (LITELLM_MASTER_KEY).")

# -----------------------
# Provider resolution helpers (kept from prior)
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

# -----------------------
# Gateway Chat endpoint (same as before) - agents will call this path on the gateway
# -----------------------
class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.0
    stream: Optional[bool] = False
    provider: Optional[str] = None
    options: Optional[Dict[str, Any]] = {}

# -- provider_generate is preserved (LiteLLM preference + fallbacks) --
# For brevity, only the part used for HTTP fallback (Gemini/Groq) is included here in minimal form.
# In this file we will directly call the gateway /v1/chat/completions, so provider_generate is
# primarily used inside the gateway endpoint. If you already run LiteLLM client inside this service,
# you can keep/extend the implementation.
# (We keep a simple delegating provider_generate to allow the gateway to call providers if needed.)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), retry=retry_if_exception_type(httpx.HTTPError))
def provider_generate(provider: Optional[str], api_key: Optional[str], model: str, prompt: str,
                      max_tokens: int = 512, temperature: float = 0.0, options: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Minimal provider_generate - prefer to be called by the gateway endpoint.
    For production, expand this to integrate with LiteLLM Python client or direct provider SDKs.
    """
    options = options or {}
    # For simplicity: raise if unknown provider so gateway can rely on configured fallbacks.
    raise HTTPException(status_code=501, detail="provider_generate in this example is a stub. Use gateway endpoint as configured.")

@app.post(OPENAI_CHAT_PATH)
async def openai_chat_endpoint(req: ChatRequest, x_api_key: Optional[str] = Header(None), authorization: Optional[str] = Header(None)):
    """
    Minimal OpenAI-compatible Chat Completions endpoint.
    This service can be used as an OpenAI-compatible gateway for agents.
    It expects agents to present LITELLM_MASTER_KEY (X-API-Key or Authorization Bearer).
    """
    if LITELLM_MASTER_KEY:
        if not _auth_header_matches_master(x_api_key, authorization):
            raise HTTPException(status_code=401, detail="Missing or invalid LITELLM_MASTER_KEY (X-API-Key or Authorization Bearer required)")

    if not req.messages:
        raise HTTPException(status_code=400, detail="messages are required")

    # Simple concatenation prompt for forward to provider in real deployment
    model = req.model or "google/gemini-1.5-flash"
    provider, api_key = resolve_provider(model, req.provider)

    # Build prompt
    parts = []
    for m in req.messages:
        r = m.get("role", "user")
        c = m.get("content", "")
        parts.append(f"[{r}] {c}")
    prompt = "\n".join(parts)

    # NOTE: In a real deployment you would call LiteLLM client or provider APIs here.
    # For this example, we'll return a canned acknowledgement so the agents can be tested.
    # Replace this with actual provider integration (e.g., litellm.Client, Gemini, or Groq).
    cid = f"chatcmpl-{uuid.uuid4().hex}"
    fake_text = f"(gateway-echo) model={model} provider={provider or 'litellm'} prompt_excerpt={prompt[:200]}"
    choice = {"index": 0, "message": {"role": "assistant", "content": fake_text}, "finish_reason": "stop"}
    out = {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
        "usage": {"prompt_tokens": 0, "completion_tokens": max(1, len(fake_text) // 4), "total_tokens": max(1, len(fake_text) // 4)}
    }
    return out

# -----------------------
# Simple local Agent implementation (fallback if CrewAI SDK not installed)
# - Agents call the cloud gateway (https://railway.app/v1/chat/completions)
# - They pass LITELLM_MASTER_KEY as X-API-Key (or Authorization Bearer)
# -----------------------
class SimpleAgent:
    def __init__(self, name: str, model: str, provider: Optional[str] = None, base_url: str = GATEWAY_CLOUD_BASE, api_key: str = LITELLM_MASTER_KEY):
        self.name = name
        self.model = model
        self.provider = provider
        self.base_url = base_url.rstrip("/")  # no trailing slash
        self.api_key = api_key

    def run_task(self, task_prompt: str, max_tokens: int = 1024, temperature: float = 0.0) -> Dict[str, Any]:
        """
        Calls the gateway's OpenAI-compatible chat completions endpoint.
        Returns the parsed JSON response.
        """
        url = f"{self.base_url}{OPENAI_COMPAT_PREFIX}/chat/completions"
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        # also send Authorization bearer as compatibility
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": task_prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "provider": self.provider  # explicit provider hint for gateway resolution
        }

        log.info("Agent %s calling gateway %s model=%s provider=%s", self.name, url, self.model, self.provider)
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                # extract assistant content if present (OpenAI-compatible)
                try:
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    content = str(data)
                return {"raw": data, "text": content}
        except Exception as exc:
            log.exception("Agent %s failed to call gateway: %s", self.name, exc)
            raise

# -----------------------
# Helper to construct real CrewAI agent or fallback SimpleAgent
# - For the "ChatOpenAI" config we set base_url to the cloud gateway: https://railway.app/v1
# -----------------------
def create_crewai_agent(name: str, model: str, provider: Optional[str] = None, agent_options: Optional[Dict] = None):
    agent_options = agent_options or {}
    # If the CrewAI SDK is available, prefer its builder (hypothetical)
    if CREWAI_AVAILABLE and AgentBuilder is not None:
        try:
            chat_openai_cfg = {
                "type": "ChatOpenAI",
                "base_url": f"{GATEWAY_CLOUD_BASE}{OPENAI_COMPAT_PREFIX}",
                "model": model,
                "api_key": LITELLM_MASTER_KEY,
            }
            builder = AgentBuilder(name=name, llm_config=chat_openai_cfg, **agent_options)
            agent = builder.build()
            log.info("Built CrewAI agent via AgentBuilder: %s", name)
            return agent
        except Exception as exc:
            log.exception("AgentBuilder failed; falling back to SimpleAgent: %s", exc)

    # If CrewAgent constructor exists:
    if CREWAI_AVAILABLE and CrewAgent is not None:
        try:
            chat_openai_cfg = {
                "type": "ChatOpenAI",
                "base_url": f"{GATEWAY_CLOUD_BASE}{OPENAI_COMPAT_PREFIX}",
                "model": model,
                "api_key": LITELLM_MASTER_KEY,
            }
            agent = CrewAgent(name=name, llm_config=chat_openai_cfg, **agent_options)
            log.info("Created CrewAgent instance: %s", name)
            return agent
        except Exception as exc:
            log.exception("CrewAgent instantiation failed; falling back: %s", exc)

    # Fallback: return our SimpleAgent which calls the gateway endpoint directly
    return SimpleAgent(name=name, model=model, provider=provider, base_url=GATEWAY_CLOUD_BASE, api_key=LITELLM_MASTER_KEY)

# -----------------------
# Bootstrapping & Crew execution endpoint
# - Creates two agents (Researcher, Writer), runs tasks in order, stores outputs.
# - Requirement #3 & #4 implemented here.
# -----------------------
@app.post("/agents/run_project")
def run_project(topic: Optional[str] = Query(None, description="Short description of project / topic")):
    """
    Bootstraps two agents and runs a small pipeline:
      1) Researcher Agent gathers and summarizes background and key points for the topic.
      2) Writer Agent uses Research output to draft a long-form marketing article.

    It persists outputs into the configured DATABASE_URL (Railway Postgres) if available.
    """
    project_topic = topic or "A large-scale software project requiring code architecture analysis and marketing collateral."
    log.info("Starting crew run for topic: %s", project_topic)

    # Create agents
    # Researcher uses Google Gemini model to gather data
    researcher = create_crewai_agent("researcher", "google/gemini-1.5-flash", provider="gemini")
    # Writer uses Llama via Groq for fast composition
    writer = create_crewai_agent("writer", "llama-3.3-70b-versatile", provider="groq")

    # Task 1: Research
    research_task_prompt = (
        f"You are a Researcher agent. Perform a comprehensive research and data-gathering task for the project topic:\n\n"
        f"{project_topic}\n\n"
        "Produce:\n"
        "- a concise summary of the project's objectives,\n"
        "- a prioritized list of technical risks and recommendations,\n"
        "- relevant references and high-level design notes that a writer can use to create marketing materials.\n"
        "Return the output in clear sections."
    )
    # Run researcher
    try:
        research_res = researcher.run_task(research_task_prompt, max_tokens=1200, temperature=0.0)
        research_text = research_res.get("text") or str(research_res.get("raw"))
    except Exception as exc:
        log.exception("Researcher failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Researcher agent error: {exc}")

    research_id = store_output("researcher", "project_research", research_text, {"agent_raw": research_res.get("raw")})

    # Task 2: Writer
    writer_task_prompt = (
        f"You are a Writer agent. Using the research summary and notes below, write a long-form marketing article (1500-2500 words) "
        f"that highlights value propositions, target audience, technical differentiators, and a call-to-action.\n\n"
        "Research notes:\n"
        f"{research_text}\n\n"
        "Formatting:\n"
        "- Start with an engaging headline and lead paragraph.\n"
        "- Use subheadings, bullet points where helpful, and include a short technical appendix.\n"
        "- End with a clear call-to-action aimed at enterprise stakeholders."
    )
    try:
        writer_res = writer.run_task(writer_task_prompt, max_tokens=2500, temperature=0.2)
        writer_text = writer_res.get("text") or str(writer_res.get("raw"))
    except Exception as exc:
        log.exception("Writer failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Writer agent error: {exc}")

    writer_id = store_output("writer", "marketing_article", writer_text, {"agent_raw": writer_res.get("raw"), "based_on": research_id})

    return {
        "ok": True,
        "topic": project_topic,
        "research": {"stored_id": research_id, "excerpt": research_text[:800]},
        "writer": {"stored_id": writer_id, "excerpt": writer_text[:800]}
    }

# Basic health endpoints
@app.get("/health")
def health():
    return {"ok": True, "service": "unified-ai-gateway", "time": int(time.time())}

@app.get("/ready")
def ready():
    return {"ok": True, "litellm_master_key_configured": bool(LITELLM_MASTER_KEY), "db": bool(engine)}

# If run as script -> run uvicorn
if __name__ == "__main__":
    log.info("Starting Unified AI Gateway & Crew runner")
    log.info("Listening on host=%s port=%s ; GATEWAY_CLOUD_BASE=%s ; DATABASE_URL=%s", HOST, PORT, GATEWAY_CLOUD_BASE, ("(configured)" if os.environ.get("DATABASE_URL") else "(fallback)"))
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, workers=int(os.environ.get("WEB_CONCURRENCY", "1")))
                     
