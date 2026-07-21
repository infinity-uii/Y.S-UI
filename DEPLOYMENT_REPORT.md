# Deployment Report — Y.S Agent System

## 1. Backend Server

### Configuration
- **Framework:** Flask 3.1.3 via Gunicorn 26.0.0 (production WSGI)
- **Entry point:** `agent_system.py` → `agent_system:app`
- **Health endpoint:** `GET /health` → `{"ok": true, "service": "agent_system", "time": "..."}`
- **API base URL:** `/api/*` (39 routes)
- **OpenAI-compatible:** `POST /v1/chat/completions`

### Production hardening applied
| Feature | Implementation |
|---|---|
| Rate limiting | flask-limiter, 200 req/min default, configurable via `RATELIMIT_STORAGE_URL` |
| CORS | flask-cors, configurable via `ALLOWED_ORIGINS` env var (comma-separated) |
| Graceful shutdown | SIGTERM/SIGINT handlers with 2s drain period |
| Error handling | Global exception handler (500), 404/405/429 handlers |
| Logging | stdout + in-memory ring buffer (2000 entries) accessible via `/api/logs` |
| Health check | `/health` endpoint + Docker HEALTHCHECK |
| Non-root user | Dockerfile runs as `appuser` |
| .env loading | python-dotenv (auto-loaded at startup) |
| HTTPS-ready | Behind any reverse proxy (Railway/nginx/Cloudflare) |

### Backend URL (local)
```
http://127.0.0.1:8080
```

### Backend URL (Railway, after deploy)
```
https://<your-railway-app>.up.railway.app
```

---

## 2. Build Artifacts

### Web Build
- **Location:** `dist/` directory
- **Files:** `index.html` (45KB), `manifest.json`, `sw.js`, `icon-192.png`, `icon-512.png`
- **Tarball:** `dist/ys-agent-web.tar.gz` (72KB)
- **Type:** Static single-file SPA (no build step required, served directly by Flask)

### Android Build (APK + AAB)
- **Config:** `capacacitor.config.json` (Capacitor 5)
- **Web assets:** `android/www/` (copies of dist files)
- **Package:** `android/package.json` (Capacitor dependencies)
- **Tarball:** `dist/ys-agent-android.tar.gz` (73KB)
- **APK output path (after build):** `android/android/app/build/outputs/apk/debug/app-debug.apk`
- **AAB output path (after build):** `android/android/app/build/outputs/bundle/release/app-release.aab`

**APK/AAB build requires Android SDK:**
```bash
cd android
npm install
npx cap sync android
cd android
./gradlew assembleDebug     # → APK
./gradlew bundleRelease      # → AAB (Play Store)
```

### Download Links
Artifacts are in the project directory:
- Web: `dist/ys-agent-web.tar.gz`
- Android: `dist/ys-agent-android.tar.gz`
- Individual files: `dist/index.html`, `dist/manifest.json`, `dist/sw.js`, `dist/icon-192.png`, `dist/icon-512.png`

**Note:** Direct download URLs require the project to be deployed to a hosting provider (Railway, Vercel, GitHub Releases, etc.). The build artifacts are ready in the `dist/` directory for upload.

---

## 3. Deployment Configurations

### Railway
- **Config file:** `railway.json`
- **Builder:** Dockerfile
- **Start command:** `gunicorn agent_system:app --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --timeout 120 --graceful-timeout 30`
- **Health check:** `/health` (30s timeout)
- **Restart policy:** ON_FAILURE (max 3 retries)

**Deploy command:**
```bash
railway up
```

### Docker
- **Dockerfile:** Python 3.12-slim, gunicorn, non-root user, healthcheck
- **docker-compose.yml:** Local dev with volume mounts and env_file

**Build and run:**
```bash
docker build -t ys-agent .
docker run -p 8080:8080 --env-file .env ys-agent
```

**Or with docker-compose:**
```bash
docker-compose up
```

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export YS_USER=admin
export YS_PASSWORD=your-password

# Run with Flask dev server
python3 agent_system.py

# Or with gunicorn (production)
gunicorn agent_system:app --bind 0.0.0.0:8080 --workers 2 --timeout 120
```

---

## 4. Required Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `YS_USER` | **Yes** | — | Admin username for login |
| `YS_PASSWORD` | **Yes** | — | Admin password for login |
| `SECRET_KEY` | Recommended | random | Flask session secret |
| `PORT` | No | `8080` | Server port |

## Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `WEB_CONCURRENCY` | `2` | Gunicorn worker count |
| `OPENAI_API_KEY` | — | OpenAI API key(s), comma-separated for rotation |
| `ANTHROPIC_API_KEY` | — | Anthropic API key(s) |
| `GEMINI_API_KEY` | — | Gemini API key(s) |
| `GROQ_API_KEY` | — | Groq API key(s) |
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama endpoint |
| `ACTIVE_PROVIDER` | `groq` | Default provider |
| `ACTIVE_MODEL` | — | Default model |
| `ALLOWED_ORIGINS` | `*` | CORS origins (comma-separated) |
| `RATELIMIT_STORAGE_URL` | `memory://` | Rate limit storage |
| `WORKSPACE_DIR` | `./workspace` | File workspace path |
| `GITHUB_TOKEN` | — | GitHub push automation |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot |
| `MCP_SERVERS` | `[]` | MCP tool servers (JSON) |
| `DATABASE_URL` | — | Database connection (falls back to in-memory) |
| `MASTER_API_KEY` | — | OpenAI-compatible endpoint auth |
| `LOG_LEVEL` | `INFO` | Logging level |
| `VITE_SUPABASE_URL` | — | Supabase URL (frontend) |
| `VITE_SUPABASE_ANON_KEY` | — | Supabase anon key (frontend) |

## Missing Optional Variables (in current .env)
- `YS_USER` — must be set for admin login
- `YS_PASSWORD` — must be set for admin login
- `SECRET_KEY` — auto-generated (set for persistent sessions)
- All provider API keys — app runs with Ollama only
- `GITHUB_TOKEN` — git push disabled without it
- `MASTER_API_KEY` — OpenAI-compatible endpoint open without it
- `DATABASE_URL` — running in-memory (data resets on restart)

---

## 5. Production Verification Results

All 16 checks passed:

| # | Check | Status |
|---|---|---|
| 1 | Health endpoint (`/health`) | PASS |
| 2 | Frontend served (`/`) | PASS |
| 3 | PWA manifest (`/manifest.json`) | PASS |
| 4 | Service worker (`/sw.js`) | PASS |
| 5 | Admin login (YS_USER/YS_PASSWORD) | PASS |
| 6 | Auth rejection (no credentials → 401) | PASS |
| 7 | Agents API (15 agents) | PASS |
| 8 | Providers API (5 providers, multi-key, health) | PASS |
| 9 | Models API | PASS |
| 10 | Plugins API (2 plugins) | PASS |
| 11 | RAG ingest + search | PASS |
| 12 | Files list | PASS |
| 13 | Admin stats | PASS |
| 14 | Admin usage | PASS |
| 15 | OpenAI-compatible endpoint | PASS |
| 16 | Rate limiting (5 rapid requests OK) | PASS |

**Gunicorn verification:** Started with 2 workers, health check OK, login OK, 15 agents loaded. PASS.

**Build script:** `build.sh` completed successfully. Web artifacts generated, Android config prepared, Python compilation verified.

---

## 6. Deployment Status

| Component | Status |
|---|---|
| Backend (Flask + Gunicorn) | Ready |
| Frontend (SPA) | Ready |
| PWA (manifest + service worker + icons) | Ready |
| Android (Capacitor config) | Ready (requires Android SDK for APK/AAB build) |
| Docker | Ready (Dockerfile + docker-compose.yml) |
| Railway | Ready (railway.json) |
| Rate limiting | Enabled |
| CORS | Configurable |
| Graceful shutdown | Implemented |
| Health check | Implemented |
| Error handling | Implemented |

---

## 7. Remaining Manual Steps

1. **Set environment variables** on Railway/Docker:
   - `YS_USER` and `YS_PASSWORD` (required for admin login)
   - `SECRET_KEY` (for persistent sessions across restarts)
   - Provider API keys (`GROQ_API_KEY`, `GEMINI_API_KEY`, etc.) for AI functionality

2. **Build APK/AAB** (requires Android SDK + Java):
   ```bash
   cd android && npm install && npx cap sync android
   cd android && ./gradlew assembleDebug    # APK
   cd android && ./gradlew bundleRelease    # AAB
   ```

3. **Deploy to Railway:**
   ```bash
   railway up
   ```
   Then set environment variables in Railway dashboard.

4. **Upload build artifacts** to a hosting provider for public download links (GitHub Releases, S3, etc.)

5. **Configure HTTPS** via Railway (automatic) or a reverse proxy (nginx/Cloudflare) for self-hosted Docker deployments.

6. **Set up a database** (optional): Set `DATABASE_URL` to a Postgres connection string for persistent conversations, jobs, and settings. Without it, the app runs in-memory mode.
