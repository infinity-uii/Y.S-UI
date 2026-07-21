# Agent System — Production AI Platform

Full-stack, production-ready AI platform with multi-agent orchestration, multi-provider LLM support, RAG, workspace, terminal, and secure authentication.

## How to run

```bash
# Set required environment variables first (see below), then:
python3 agent_system.py
```

Or use the configured Replit workflow: **Start application** (`PORT=5000 python3 agent_system.py`)

## Required environment variables

| Variable | Description |
|---|---|
| `YS_USER` | Admin login username |
| `YS_PASSWORD` | Admin login password |
| `SESSION_SECRET` | Flask session secret (any random string) |

## Optional environment variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub personal access token for auto-push |
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GROQ_API_KEY` | Groq API key |
| `OLLAMA_BASE` | Ollama base URL (default: `http://localhost:11434`) |
| `PORT` | Server port (default: `8080`, set to `5000` for Replit webview) |
| `WORKSPACE_DIR` | Directory for uploaded files (default: `./workspace`) |

## Architecture

### Backend (`agent_system.py`)

- **Flask** web server with SSE streaming, threaded
- **Provider abstraction**: `ProviderBase` → Ollama / OpenAI / Anthropic / Gemini / Groq / OpenAI-compatible
- **Default**: Ollama `llama3.2` at `http://localhost:11434/v1/chat/completions`
- **Agents**: 7 specialized agents (Architect, Reviewer, Executor, GitHub, Research, RAG, Deployment), each with system prompt, role, permissions, tools
- **Plugins**: Extensible `PluginBase` class — pre-loaded: `web_search`, `python_exec`
- **RAG**: `RAGManager` with `RAGBackendBase` interface — default in-memory, swap to ChromaDB/Qdrant/PGVector/Milvus
- **Auth**: Session-based + API key header (`X-API-Key`)
- **Terminal**: Subprocess streaming via NDJSON
- **GitHub**: Auto git add/commit/push via `GITHUB_TOKEN`

### Frontend (`index.html`)

Single-file SPA, no build step required.

- Dark theme, responsive
- Pages: Chat, Agents, Compare Models, Knowledge/RAG, Plugins, Admin, Settings
- Right panel: Workspace file explorer, Terminal, Logs, Preview iframe
- Streaming SSE responses with live markdown + syntax highlighting
- Drag & drop file uploads
- Conversation management (folders, tags, favorites)

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (no auth) |
| GET/POST | `/login` | Authentication |
| GET | `/api/providers` | List AI providers |
| POST | `/api/providers/switch` | Switch active provider |
| GET | `/api/models` | List models for provider |
| POST | `/api/models/switch` | Switch active model |
| POST | `/api/chat` | Chat (non-streaming) |
| POST | `/api/chat/stream` | Chat (SSE streaming) |
| GET/POST | `/api/conversations` | List / create conversations |
| GET/PATCH/DELETE | `/api/conversation/<id>` | Get / update / delete |
| GET | `/api/agents` | List agents |
| POST | `/api/agents/<name>/run` | Run agent async |
| GET | `/api/jobs/<id>` | Poll job status |
| POST | `/api/trigger` | Trigger agent pipeline |
| GET | `/api/files/list` | List workspace files |
| GET | `/api/files/read` | Read file content |
| POST | `/api/files/upload` | Upload files |
| DELETE | `/api/files/delete` | Delete file |
| POST | `/api/terminal/exec` | Execute terminal command (streaming NDJSON) |
| GET | `/api/plugins` | List plugins |
| POST | `/api/plugins/<name>/run` | Run plugin |
| POST | `/api/search` | Web search (DuckDuckGo) |
| POST | `/api/rag/ingest` | Ingest text into knowledge base |
| POST | `/api/rag/search` | Semantic search |
| POST | `/api/compare` | Compare multiple models |
| GET | `/api/logs` | Application logs |
| POST | `/api/trigger` | Pipeline trigger |
| POST | `/api/github/push` | Git add/commit/push |
| GET | `/api/admin/stats` | Usage statistics |
| GET | `/api/admin/usage` | Provider/model usage |
| POST | `/v1/chat/completions` | OpenAI-compatible endpoint |

## Deployment

### Railway
Set `PORT`, `YS_USER`, `YS_PASSWORD`, `SESSION_SECRET` in Railway environment. Connect GitHub repo and deploy.

### Docker
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install flask requests werkzeug
CMD ["python3", "agent_system.py"]
```

### Kubernetes / Helm
Configure as a standard web deployment with env vars from Secrets. Set `PORT=8080`, expose via Service/Ingress.

## RAG backends (production)

The in-memory RAG backend is reset on restart. For persistence, implement `RAGBackendBase` with:

- **ChromaDB**: `CHROMA_HOST` + `CHROMA_PORT`
- **Qdrant**: `QDRANT_URL` + `QDRANT_API_KEY`
- **PGVector**: `PGVECTOR_URL` (PostgreSQL with pgvector extension)
- **Milvus**: `MILVUS_HOST` + `MILVUS_PORT`

## Architecture enhancements (completed)

- **Gateway module** (`gateway/`): Unified AI adapter pattern, request validation middleware, secure key injection, audit logging
- **Login overlay**: Full session-based auth UI added to `index.html` — auto-detects 401, shows login form
- **`/api/auth/status`**: Frontend uses this to check session state without a destructive 401
- **Telegram bot** (`telegram_bot.py`): Full Arabic inline keyboard UI with all platform features
- **Workflow**: Port-kill prefix in run command prevents "address in use" errors

## User preferences

- Preserve existing project architecture — no restructuring
- No placeholder code, no TODO comments
- Default providers: Groq (priority 100) → Gemini (priority 90) → OpenAI/Anthropic (50) → Ollama (10)
- Commit and push changes automatically via GITHUB_TOKEN
