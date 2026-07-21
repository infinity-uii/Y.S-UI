# Configuration loader
import os
from typing import Optional

class Settings:
    DATABASE_URL: Optional[str]
    SECRET_KEY: str
    YS_USER: str
    YS_PASSWORD: str
    OLLAMA_BASE: str
    WORKSPACE_DIR: str
    LOG_LEVEL: str
    MCP_SERVERS: str
    GOOGLE_SEARCH_API_KEY: str
    GOOGLE_SEARCH_ENGINE_ID: str
    TELEGRAM_BOT_TOKEN: str

    def __init__(self):
        self.DATABASE_URL = os.environ.get("DATABASE_URL")
        self.SECRET_KEY = (
            os.environ.get("SESSION_SECRET")
            or os.environ.get("SECRET_KEY")
            or os.environ.get("SECRET", "")
        )
        self.MASTER_API_KEY = os.environ.get("MASTER_API_KEY", "")
        self.GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", self.MASTER_API_KEY)
        self.YS_USER = os.environ.get("YS_USER", "")
        self.YS_PASSWORD = os.environ.get("YS_PASSWORD", "")
        self.OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
        self.WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "./workspace")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
        self.MCP_SERVERS = os.environ.get("MCP_SERVERS", "")
        self.GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        self.GOOGLE_SEARCH_ENGINE_ID = os.environ.get("GOOGLE_SEARCH_ENGINE_ID", "")
        self.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

settings = Settings()
