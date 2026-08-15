import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# All application tables are created with this prefix so the schema can share a
# MySQL/PostgreSQL instance with other services without name collisions.
TABLE_PREFIX = os.environ.get("APP_TABLE_PREFIX", "agentar_mem_app_")


def _build_database_url() -> str:
    """Build the application database URL from environment variables.

    Backend is selected with APP_DB_BACKEND:
    - "postgres" (default): postgresql+psycopg, falls back to POSTGRES_* vars.
    - "mysql" (MySQL 8 compatible): mysql+pymysql, falls back to MYSQL_* vars.
    APP_DB_HOST/PORT/USER/PASSWORD/NAME override the backend-specific defaults.
    """
    backend = os.environ.get("APP_DB_BACKEND", "postgres").strip().lower()

    if backend in ("mysql", "mysql8", "mariadb"):
        host = os.environ.get("APP_DB_HOST") or os.environ.get("MYSQL_HOST", "mysql")
        port = os.environ.get("APP_DB_PORT") or os.environ.get("MYSQL_PORT", "3306")
        user = os.environ.get("APP_DB_USER") or os.environ.get("MYSQL_USER", "mem0")
        password = os.environ.get("APP_DB_PASSWORD") or os.environ.get("MYSQL_PASSWORD", "mem0")
        db = os.environ.get("APP_DB_NAME") or os.environ.get("MYSQL_DATABASE", "mem0_app")
        return f"mysql+pymysql://{user}:{quote_plus(password)}@{host}:{port}/{db}?charset=utf8mb4"

    host = os.environ.get("APP_DB_HOST") or os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("APP_DB_PORT") or os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("APP_DB_USER") or os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("APP_DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "postgres")
    db = os.environ.get("APP_DB_NAME", "mem0_app")
    return f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{db}"


def _engine_kwargs() -> dict:
    kwargs = {"pool_pre_ping": True}
    if _build_database_url().startswith("mysql"):
        # MySQL closes idle connections after wait_timeout; recycle proactively.
        kwargs["pool_recycle"] = 3600
    return kwargs


engine = create_engine(_build_database_url(), **_engine_kwargs())

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a SQLAlchemy session."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
