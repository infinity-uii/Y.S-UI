- Added DB integration using SQLAlchemy (sync engine). Auto-initialization is performed if DATABASE_URL env var is set. For full migrations use Alembic.
- Files added:
  - config.py
  - db/session.py
  - db/models.py
  - db/repos.py
  - db/migrations.py

Integration notes:
- The DB layer uses SQLAlchemy's sync engine by default (DATABASE_URL expected like postgresql://user:pass@host:5432/dbname).
- If you prefer async engine, set DATABASE_URL to a SQLAlchemy async URL (postgresql+asyncpg://...). The init code will try to create an async engine (limited support).
