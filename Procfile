web: bash -lc 'gunicorn agent_system:app --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -'
