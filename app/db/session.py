from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models.base import Base

_engines = {}

def normalize_database_url(database_url: str) -> str:
    return database_url if "://" in database_url else f"sqlite:///{Path(database_url).resolve().as_posix()}"

def get_engine(database_url: str):
    database_url = normalize_database_url(database_url)
    if database_url not in _engines:
        connect_args = {"check_same_thread": False, "timeout": 5} if database_url.startswith("sqlite") else {}
        engine = create_engine(database_url, connect_args=connect_args, future=True)
        if database_url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def set_sqlite_pragmas(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
        _engines[database_url] = engine
    return _engines[database_url]

def create_schema(database_url: str):
    database_url = normalize_database_url(database_url)
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///" )).parent.mkdir(parents=True, exist_ok=True)
    from app import models  # noqa: F401
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(generation_jobs)")
            }
            if "cancel_requested_at" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE generation_jobs ADD COLUMN cancel_requested_at DATETIME"
                )

@contextmanager
def session_scope(database_url: str):
    session = sessionmaker(bind=get_engine(database_url), expire_on_commit=False, future=True)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback(); raise
    finally:
        session.close()
