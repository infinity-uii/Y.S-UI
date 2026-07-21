# Dockerfile — Production-ready for Railway, Docker, and local Docker dev
# Backend: Flask (agent_system.py) via gunicorn
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PORT=8080

WORKDIR /app

# System deps for build + runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies (layer cached)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY . /app

# Create workspace + uploads directories
RUN mkdir -p /app/workspace/uploads

# Non-root user for security
RUN useradd --no-create-home --shell /usr/sbin/nologin appuser 2>/dev/null || true
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Health check (Railway + Docker)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Graceful shutdown via gunicorn --graceful-timeout
CMD ["bash", "-lc", "gunicorn agent_system:app --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -"]
