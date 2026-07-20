"""Migration helper: simple automatic initialization and rudimentary migrations.
This is intentionally conservative: it will create missing tables via metadata.create_all.
For production migrations, use Alembic and a proper migration workflow.
"""
from db.session import init_db_engine, create_all, ensure_connection
import logging

log = logging.getLogger("migrations")


def prepare_database():
    init_db_engine(echo=False)
    ok = ensure_connection(retries=5, delay=2.0)
    if not ok:
        log.warning("Database not available during startup (prepare_database)")
        return False
    create_all()
    log.info("Database initialization complete")
    return True
