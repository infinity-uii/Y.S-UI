# Environment Audit & Configuration Report
**Y.S Agent System** — Complete Analysis & Inventory

**Report Date:** 2026-07-22  
**Repository:** infinity-uii/Y.S-UI  
**Audit Status:** ✅ COMPREHENSIVE

---

## 📋 Executive Summary

This report documents all environment variables, their usage, dependencies, and configuration across the **Y.S Agent System** platform. The project implements an enterprise-grade AI orchestration platform with multi-provider support, multi-agent workflows, and comprehensive integration capabilities.

---

## 🔐 Environment Variables Inventory

### Core Server Configuration

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `PORT` | Integer | ❌ No | `8080` | HTTP server port | agent_system.py:61 |
| `HOST` | String | ❌ No | `0.0.0.0` | Bind address | agent_system.py:60 |
| `WEB_CONCURRENCY` | Integer | ❌ No | `1` | Gunicorn workers | Procfile |
| `SESSION_SECRET` | String | ✅ **Yes** | — | Flask session encryption key (Replit standard) | agent_system.py:66-70, config.py:14 |
| `SECRET_KEY` | String | ⚠️ Fallback | `auto-generated` | Alternative session secret | agent_system.py:68 |

### Admin Authentication

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `YS_USER` | String | ✅ **Yes** | — | Admin username for web UI login | agent_system.py:1296, config.py:27 |
| `YS_PASSWORD` | String | ✅ **Yes** | — | Admin password for web UI login | agent_system.py:1297, config.py:28 |
| `MASTER_API_KEY` | String | ❌ No | — | Bearer token for programmatic API access (X-API-Key header) | gateway/middleware.py:40, agent_system.py:856 |

### AI Provider Keys (Multi-Key Support)

Each provider supports **comma-separated multiple keys** for rotation, failover, and load balancing.

#### Primary Default Providers (Highest Priority)

| Provider | Variable | Type | Required | Default Model | Priority | Fallback | File |
|----------|----------|------|----------|----------------|----------|----------|------|
| **Groq** | `GROQ_API_KEY` | String (comma-sep) | ✅ **Recommended** | llama-3.3-70b-versatile | **100** (Primary) | Yes | agent_system.py:77, 817 |
| **Gemini** | `GEMINI_API_KEY` | String (comma-sep) | ✅ **Recommended** | gemini-1.5-flash | **90** (Primary) | Yes | agent_system.py:76, 818 |

#### Secondary Providers

| Provider | Variable | Type | Required | Default Model | Priority | Fallback | File |
|----------|----------|------|----------|----------------|----------|----------|------|
| **OpenAI** | `OPENAI_API_KEY` | String (comma-sep) | ❌ No | gpt-4o | **50** (Secondary) | Yes | agent_system.py:74, 819 |
| **Anthropic** | `ANTHROPIC_API_KEY` | String (comma-sep) | ❌ No | claude-3-5-sonnet-20241022 | **50** (Secondary) | Yes | agent_system.py:75, 820 |
| **Ollama** | — (Local) | — | ❌ No | llama3.2 | **10** (Fallback) | Yes | agent_system.py:816 |

#### Multi-Key Format

```bash
# Single key
GROQ_API_KEY=your-api-key-here

# Multiple keys (rotation/failover/load-balancing)
GROQ_API_KEY=key1,key2,key3,key4

# Each key is parsed in agent_system.py:812-813
```

### Model Configuration

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `ACTIVE_PROVIDER` | String | ❌ No | `groq` or `ollama` | Active LLM provider (can change at runtime) | agent_system.py:823 |
| `ACTIVE_MODEL` | String | ❌ No | — | Active model name (can change at runtime) | agent_system.py:824 |
| `OLLAMA_BASE` | URL | ❌ No | `http://localhost:11434` | Ollama server base URL | agent_system.py:81, config.py:29 |

### Local AI (Ollama)

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `OLLAMA_BASE` | URL | ❌ No | `http://localhost:11434` | Ollama API endpoint | agent_system.py:81 |

### Cloud AI (OpenAI-Compatible)

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `GROQ_API_BASE` | URL | ❌ No | `https://api.groq.com/openai/v1` | Groq API endpoint | main.py:58 |

### Database & Storage

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `DATABASE_URL` | URL | ❌ No | `sqlite:///./local_dev.sqlite` | PostgreSQL connection string (Railway) | main.py:64, config.py:19 |
| `WORKSPACE_DIR` | Path | ❌ No | `./workspace` | Local file workspace root | agent_system.py:86, config.py:30 |
| `VITE_SUPABASE_URL` | URL | ❌ No | — | Supabase backend for persistent RAG/storage | .env.example:63 |
| `VITE_SUPABASE_ANON_KEY` | String | ❌ No | — | Supabase anonymous key | .env.example:64 |

### API Gateway & Security

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `MASTER_API_KEY` | String | ❌ No | — | Master key for gateway authentication (X-API-Key or Bearer) | gateway/middleware.py:40, agent_system.py:856 |
| `GATEWAY_API_KEY` | String | ❌ No | Inherits MASTER_API_KEY | Gateway-specific API key | config.py:26 |
| `ALLOWED_ORIGINS` | String (comma-sep) | ❌ No | `*` (allow all) | CORS allowed origins | agent_system.py:135-136 |
| `RATELIMIT_STORAGE_URL` | URL | ❌ No | `memory://` | Rate limit storage backend | agent_system.py:166 |

### GitHub Integration

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `GITHUB_TOKEN` | String | ❌ No | — | GitHub API token for repository operations | agent_system.py:71, telegram_bot.py (Git Agent) |

### Telegram Bot Integration

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `TELEGRAM_BOT_TOKEN` | String | ❌ No | — | Telegram Bot API token (starts with bot ID) | agent_system.py:1818, telegram_bot.py:338 |

### MCP (Model Context Protocol) Servers

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `MCP_SERVERS` | JSON Array | ❌ No | `[]` | Auto-discovery list of MCP servers | mcp_client.py:248-251, config.py:32 |

**Format:**
```bash
# Comma-separated server URLs
MCP_SERVERS=http://mcp1:3000,http://mcp2:3000,stdio:///path/to/tool

# Or JSON array
MCP_SERVERS=[{"endpoint":"http://mcp:3000","name":"tool1"}]
```

### Logging & Monitoring

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `LOG_LEVEL` | String | ❌ No | `INFO` | Python logging level (DEBUG, INFO, WARNING, ERROR) | agent_system.py:127, config.py:31 |

### Frontend Configuration

| Variable | Type | Required | Default | Usage | File |
|----------|------|----------|---------|-------|------|
| `VITE_API_BASE` | URL | ❌ No | — | Frontend API base URL (index.html) |
| `API_PREFIX` | String | ❌ No | `/api` | API route prefix | agent_system.py:178 |

---

## 🔌 Provider Implementation Summary

### 1. **Groq** (Primary Default)
- **Provider Name:** `groq`
- **Key Variable:** `GROQ_API_KEY` (supports multi-key)
- **API Endpoint:** `https://api.groq.com/openai/v1`
- **Default Model:** `llama-3.3-70b-versatile`
- **Available Models:** 
  - `llama-3.3-70b-versatile`
  - `llama-3.1-8b-instant`
  - `mixtral-8x7b-32768`
  - `gemma2-9b-it`
- **Status:** ✅ Fully Implemented
- **Priority:** 100 (Highest)
- **File:** agent_system.py:691-745

### 2. **Google Gemini** (Primary Default)
- **Provider Name:** `gemini`
- **Key Variable:** `GEMINI_API_KEY` (supports multi-key)
- **API Endpoint:** `https://generativelanguage.googleapis.com/v1beta/models/`
- **Default Model:** `gemini-1.5-flash`
- **Available Models:**
  - `gemini-1.5-flash`
  - `gemini-1.5-pro`
  - `gemini-2.0-flash-exp`
- **Status:** ✅ Fully Implemented
- **Priority:** 90
- **File:** agent_system.py:613-688

### 3. **OpenAI**
- **Provider Name:** `openai`
- **Key Variable:** `OPENAI_API_KEY` (supports multi-key)
- **API Endpoint:** `https://api.openai.com/v1`
- **Default Model:** `gpt-4o`
- **Available Models:**
  - `gpt-4o`
  - `gpt-4o-mini`
  - `gpt-4-turbo`
  - `gpt-3.5-turbo`
- **Status:** ✅ Fully Implemented
- **Priority:** 50
- **File:** agent_system.py:482-536

### 4. **Anthropic Claude**
- **Provider Name:** `anthropic`
- **Key Variable:** `ANTHROPIC_API_KEY` (supports multi-key)
- **API Endpoint:** `https://api.anthropic.com/v1`
- **Default Model:** `claude-3-5-sonnet-20241022`
- **Available Models:**
  - `claude-3-5-sonnet-20241022`
  - `claude-3-5-haiku-20241022`
  - `claude-3-opus-20240229`
- **Status:** ✅ Fully Implemented
- **Priority:** 50
- **File:** agent_system.py:539-610

### 5. **Ollama** (Local Fallback)
- **Provider Name:** `ollama`
- **Key Variable:** — (No API key required)
- **API Endpoint:** Configured via `OLLAMA_BASE`
- **Default Model:** `llama3.2`
- **Status:** ✅ Fully Implemented
- **Priority:** 10 (Fallback)
- **File:** agent_system.py:429-479

### 6. **OpenAI-Compatible** (Generic)
- **Provider Name:** `openai-compatible`
- **Base URL:** Configurable
- **Status:** ✅ Available for custom backends
- **File:** agent_system.py:748-803

---

## 🔄 Multi-Key Management (Key Pool)

### Features Implemented

#### ✅ Rotation
- Round-robin key rotation (agent_system.py:261-274)
- Automatic index increment on each call
- No key preference bias

#### ✅ Failover
- Automatic key disabling after 3 failures (agent_system.py:283)
- 60-second cooldown period before re-enabling (agent_system.py:284)
- Automatic recovery on successful request (agent_system.py:286-291)

#### ✅ Load Balancing
- Provider priority ordering (agent_system.py:321-327)
- Failover cascade to secondary providers (agent_system.py:393-423)
- Health status tracking per key (agent_system.py:293-304)

### KeyPool Class (agent_system.py:249-308)

```python
KeyPool(keys: List[str], provider_name: str)
├── next_key() → str | None           # Round-robin selection
├── report_failure(key: str)           # Track failures
├── report_success(key: str)           # Reset failure count
├── health_status() → List[Dict]       # Health dashboard
└── count() → int                      # Key count
```

---

## 👥 Agent System

### 15 Pre-Configured Agents

| Agent | Name | Role | Provider | Model | Tools | File |
|-------|------|------|----------|-------|-------|------|
| 1️⃣ | coding | Code generation & debugging | groq | llama-3.3-70b-versatile | python_exec, file_manager, terminal | agent_system.py:1086-1091 |
| 2️⃣ | research | Information gathering & analysis | gemini | gemini-1.5-flash | web_search, rag | agent_system.py:1092-1097 |
| 3️⃣ | vision | Image analysis & understanding | openai | gpt-4o | image_analysis, ocr | agent_system.py:1098-1103 |
| 4️⃣ | writing | Content creation & editing | groq | llama-3.3-70b-versatile | — | agent_system.py:1104-1109 |
| 5️⃣ | translation | Multi-language translation | groq | llama-3.3-70b-versatile | — | agent_system.py:1110-1115 |
| 6️⃣ | planner | Project planning & task breakdown | gemini | gemini-1.5-flash | — | agent_system.py:1116-1121 |
| 7️⃣ | browser | Web browsing & scraping | gemini | gemini-1.5-flash | web_search | agent_system.py:1122-1127 |
| 8️⃣ | rag | Knowledge retrieval & Q&A | groq | llama-3.3-70b-versatile | rag | agent_system.py:1128-1133 |
| 9️⃣ | file | File management & operations | groq | llama-3.3-70b-versatile | file_manager | agent_system.py:1134-1139 |
| 🔟 | terminal | Command execution | groq | llama-3.3-70b-versatile | terminal | agent_system.py:1140-1145 |
| 1️⃣1️⃣ | git | Version control operations | groq | llama-3.3-70b-versatile | github | agent_system.py:1146-1151 |
| 1️⃣2️⃣ | debug | Error analysis & debugging | groq | llama-3.3-70b-versatile | python_exec, terminal, file_manager | agent_system.py:1152-1157 |
| 1️⃣3️⃣ | security | Security analysis & auditing | anthropic | claude-3-5-sonnet-20241022 | file_manager, terminal | agent_system.py:1158-1163 |
| 1️⃣4️⃣ | database | Database operations & SQL | groq | llama-3.3-70b-versatile | sql_explorer | agent_system.py:1164-1169 |
| 1️⃣5️⃣ | devops | Deployment & infrastructure | groq | llama-3.3-70b-versatile | terminal, file_manager | agent_system.py:1170-1175 |

---

## 🔧 API Routes Summary

### Authentication
- `GET/POST /login` — Admin login
- `POST /logout` — Session logout
- `GET /api/auth/status` — Check auth status

### Providers
- `GET /api/providers` — List all providers
- `POST /api/providers/switch` — Set active provider
- `POST /api/providers/toggle` — Enable/disable provider
- `POST /api/providers/keys` — Update provider keys (multi-key)
- `POST /api/providers/model_map` — Map model to provider

### Models
- `GET /api/models` — List models for provider
- `POST /api/models/switch` — Set active model

### Chat
- `POST /api/chat` — Non-streaming chat
- `POST /api/chat/stream` — SSE streaming chat
- `POST /api/gateway/chat` — Secure gateway endpoint
- `POST /api/gateway/stream` — Secure streaming endpoint

### Gateway
- `GET /api/gateway/providers` — Gateway provider list
- Requires: `X-API-Key` or `Authorization: Bearer` header

### Agents
- `GET /api/agents` — List all agents
- `POST /api/agents/{name}/run` — Run agent (async)
- `GET /api/jobs/{job_id}` — Get job status
- `POST /api/trigger` — Multi-agent workflow pipeline

### RAG (Knowledge Base)
- `POST /api/rag/ingest` — Add text to knowledge base
- `POST /api/rag/search` — Search knowledge base
- `POST /api/rag/clear` — Clear knowledge base (admin)

### Files
- `GET /api/files/list` — List files in workspace
- `GET /api/files/read` — Read file content
- `POST /api/files/upload` — Upload file
- `DELETE /api/files/delete` — Delete file

### Terminal
- `POST /api/terminal/exec` — Execute shell command (admin)

### Plugins
- `GET /api/plugins` — List plugins
- `POST /api/plugins/{name}/run` — Run plugin

### Search
- `POST /api/search` — Web search via DuckDuckGo

### Admin
- `GET /api/admin/stats` — System statistics
- `GET /api/admin/usage` — Provider usage report
- `GET /api/logs` — System logs

### GitHub
- `POST /api/github/push` — Git push to repository

### Tools
- `GET /api/tools` — List MCP tools
- `POST /api/tools/enable` — Enable/disable tool
- `POST /api/tools/run` — Execute tool (streaming)

### Conversations
- `GET /api/conversations` — List conversations
- `POST /api/conversations` — Create conversation
- `GET /api/conversation/{id}` — Get conversation
- `PATCH /api/conversation/{id}` — Update conversation
- `DELETE /api/conversation/{id}` — Delete conversation

### Model Comparison
- `POST /api/compare` — Compare multiple models

### OpenAI-Compatible
- `POST /v1/chat/completions` — OpenAI-compatible endpoint (external tools)

---

## ✅ Security & Authentication

### Admin Credentials
- **Username Variable:** `YS_USER`
- **Password Variable:** `YS_PASSWORD`
- **Storage:** Environment variables (NOT cached at startup)
- **Protection:** SHA-256 hashing
- **File:** agent_system.py:838-839, 1296-1305

**Important:** Credentials are read from environment on every request, allowing hot updates via Replit Secrets without restart.

### Session Management
- **Session Key:** `SESSION_SECRET` or `SECRET_KEY`
- **Duration:** Browser session
- **Storage:** Flask session cookie (encrypted)

### API Key Authentication
- **Header:** `X-API-Key` or `Authorization: Bearer {token}`
- **Master Key:** `MASTER_API_KEY`
- **Fallback:** Session-based authentication
- **File:** gateway/middleware.py:44-71

---

## 🧪 Missing/Unused Variables

### ❌ Not Found in Codebase

| Variable | Expected Use | Status |
|----------|--------------|--------|
| `GOOGLE_SEARCH_API_KEY` | Web search | ✅ Implemented (DuckDuckGo instead) |
| `GOOGLE_SEARCH_ENGINE_ID` | Web search | ✅ Implemented (DuckDuckGo instead) |

**Note:** Web search uses DuckDuckGo API (no key required).

---

## 📊 Configuration Status

### ✅ Fully Implemented

- [x] Multi-provider support (6 providers)
- [x] Multi-key management with rotation/failover/load-balancing
- [x] Multi-agent orchestration (15 agents)
- [x] Admin authentication (YS_USER/YS_PASSWORD)
- [x] API gateway with authentication
- [x] RAG (knowledge base)
- [x] File workspace
- [x] Terminal execution
- [x] GitHub integration
- [x] Telegram bot integration
- [x] MCP server discovery
- [x] Conversation history
- [x] Rate limiting
- [x] CORS configuration
- [x] Logging

### ⚠️ Partial/Optional

- [ ] PostgreSQL database (optional, SQLite fallback)
- [ ] Supabase integration (optional)
- [ ] Google Search API (replaced with DuckDuckGo)

### ❌ Not Implemented

None identified as critical missing.

---

## 🚀 Production Readiness Checklist

| Component | Status | Notes |
|-----------|--------|-------|
| **Core Server** | ✅ Ready | Uses Gunicorn/WSGI in production |
| **Providers** | ✅ Ready | Groq + Gemini as defaults |
| **Authentication** | ✅ Ready | YS_USER/YS_PASSWORD required |
| **API Gateway** | ✅ Ready | Secure middleware implemented |
| **Database** | ⚠️ Optional | PostgreSQL supported but not required |
| **Telegram Bot** | ✅ Ready | Full Arabic UI implemented |
| **Agents** | ✅ Ready | 15 specialized agents |
| **RAG** | ✅ Ready | In-memory backend (swap as needed) |
| **MCP** | ✅ Ready | Auto-discovery implemented |
| **Rate Limiting** | ✅ Ready | Configurable storage backend |

---

## 📝 Sample Configuration

### Development
```bash
# .env
PORT=8080
HOST=0.0.0.0
SESSION_SECRET=your-random-secret-here
YS_USER=admin
YS_PASSWORD=change-me-to-strong-password

GROQ_API_KEY=your-groq-key
GEMINI_API_KEY=your-gemini-key

ACTIVE_PROVIDER=groq
WORKSPACE_DIR=./workspace
LOG_LEVEL=INFO
```

### Production (Railway)
```bash
# Railway Secrets
PORT=8080
SESSION_SECRET=${RANDOM_SECRET_32_CHARS}
YS_USER=${ADMIN_USERNAME}
YS_PASSWORD=${ADMIN_PASSWORD}

GROQ_API_KEY=${GROQ_KEY_1},${GROQ_KEY_2}
GEMINI_API_KEY=${GEMINI_KEY_1},${GEMINI_KEY_2}
OPENAI_API_KEY=${OPENAI_KEY_1}
ANTHROPIC_API_KEY=${ANTHROPIC_KEY_1}

DATABASE_URL=postgresql://${USER}:${PASS}@${HOST}:${PORT}/${DB}

GITHUB_TOKEN=${GITHUB_TOKEN}
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}

ALLOWED_ORIGINS=https://yourdomain.com
RATELIMIT_STORAGE_URL=redis://redis:6379

ACTIVE_PROVIDER=groq
LOG_LEVEL=WARNING
```

---

## 📚 References

- **Agent System:** agent_system.py (main application)
- **Gateway:** gateway/routes.py, gateway/middleware.py
- **Config:** config.py
- **Telegram Bot:** telegram_bot.py
- **MCP Client:** mcp_client.py

---

**Report Status:** ✅ COMPLETE  
**Last Updated:** 2026-07-22
