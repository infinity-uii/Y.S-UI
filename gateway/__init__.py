# gateway — Secure AI API Gateway
# Provides unified adapter pattern, request validation middleware,
# and secure key injection for all AI provider calls.
from .routes import gateway_bp

__all__ = ["gateway_bp"]
