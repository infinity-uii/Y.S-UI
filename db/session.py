"""
Database initialization and session management.
Supports optional async engine (postgresql+asyncpg) and a sync engine fallback.
"""
from __future__ import annotations

import os
import time
import logging
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from sqlalchemy.orm import declarative_base

log = logging.getLogger("db")

DATABASE_URL = os.environ.get("DATABASE_URL")

Base = declarative_base()

_sync_engine = None
_async_engine: Optional[AsyncEngine] = None
SessionLocal = None
AsyncSessionLocal = None


def init_db_engine(echo: bool = False):
    global _sync_engine, _async_engine, SessionLocal, AsyncSessionLocal
    if not DATABASE_URL:
        log.info("DATABASE_URL not set — skipping DB initialization")
        return
    # Decide sync vs async
    if DATABASE_URL.startswith("postgresql+asyncpg://"):
        # Async engine
        _async_engine = create_async_engine(DATABASE_URL, echo=echo, pool_pre_ping=True)
        AsyncSessionLocal = sessionmaker(bind=_async_engine, class_=None, expire_on_commit=False)
        log.info("Initialized async DB engine")
    else:
        # Sync engine
        _sync_engine = create_engine(DATABASE_URL, echo=echo, pool_pre_ping=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_sync_engine)
        log.info("Initialized sync DB engine")


def get_sync_engine():
    return _sync_engine


def get_session():
    global SessionLocal
    if SessionLocal is None:
        raise RuntimeError("DB Session not initialized")
    return SessionLocal()


def ensure_connection(retries: int = 3, delay: float = 2.0) -> bool:
    """Attempt to connect to the DB with retries. Returns True if successful."""
    engine = _sync_engine
    if engine is None:
        log.info("No sync engine configured")
        return False
    attempt = 0
    while attempt < retries:
        try:
            conn = engine.connect()
            conn.close()
            return True
        except OperationalError as exc:
            attempt += 1
            log.warning("DB connection failed (attempt %s/%s): %s", attempt, retries, exc)
            time.sleep(delay)
    return False


def create_all():
    """Create all tables (simple automatic initialization)."""
    engine = _sync_engine
    if engine is None:
        log.info("No sync engine configured — skipping create_all")
        return
    Base.metadata.create_all(bind=engine)
    log.info("Executed Base.metadata.create_all")
