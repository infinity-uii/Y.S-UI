# Dockerfile - production-ready for Railway
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# Minimal system deps for wheel builds (kept small)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements early for Docker layer caching
COPY requirements.txt /app/requirements.txt

# Upgrade pip & install requirements
RUN python -m pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY . /app

# Use a non-root user
RUN useradd --no-create-home --shell /usr/sbin/nologin appuser || true
USER appuser

# Expose (Railway will set PORT env var)
EXPOSE 8080

# Run via uvicorn; use env PORT if provided by platform
CMD ["bash", "-lc", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${WEB_CONCURRENCY:-1}"]
