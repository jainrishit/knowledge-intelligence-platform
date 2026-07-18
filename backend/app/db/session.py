"""Database session management. Swap DATABASE_URL to change the backing database."""
import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from typing import Generator

from app.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _apply_column_migrations() -> None:
    """
    Add any ORM-defined columns that are missing from the live database.

    SQLAlchemy's create_all() only creates missing *tables* — it never alters
    existing ones.  This function performs a safe ALTER TABLE ADD COLUMN for any
    column that is present in the ORM model but absent from the DB table.

    Only supports adding nullable columns (safe for existing rows).
    Called once at startup, before the graph is loaded.
    """
    inspector = inspect(engine)
    with engine.connect() as conn:
        for table_name, table in Base.metadata.tables.items():
            if not inspector.has_table(table_name):
                continue  # create_all will handle new tables
            existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name not in existing_cols:
                    # Build the SQL type string
                    col_type = col.type.compile(engine.dialect)
                    nullable = "" if col.nullable else " NOT NULL"
                    default_clause = ""
                    if col.default is not None and col.default.is_scalar:
                        default_clause = f" DEFAULT {col.default.arg!r}"
                    sql = (
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {col.name} {col_type}{default_clause}"
                    )
                    try:
                        conn.execute(text(sql))
                        conn.commit()
                        logger.info(
                            "[db_migration] Added column %s.%s (%s)",
                            table_name, col.name, col_type,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[db_migration] Could not add column %s.%s: %s",
                            table_name, col.name, exc,
                        )


def init_db() -> None:
    """Create all tables and apply any missing column migrations on startup."""
    Base.metadata.create_all(bind=engine)
    _apply_column_migrations()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
