from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from app.models.base import Base

_engines = {}
_DATABASE_INITIALIZATION_LOCK_TIMEOUT_SECONDS = 30.0
_DATABASE_INITIALIZATION_LOCK_RETRY_SECONDS = 0.05

def normalize_database_url(database_url: str) -> str:
    return database_url if "://" in database_url else f"sqlite:///{Path(database_url).resolve().as_posix()}"

def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(normalize_database_url(database_url))
    if url.get_backend_name() != "sqlite":
        return None
    if not url.database or url.database == ":memory:" or url.query.get("mode") == "memory":
        return None
    return Path(url.database).resolve()

def _try_lock_file(lock_file) -> bool:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {33, 36}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True

def _unlock_file(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

@contextmanager
def _database_initialization_lock(database_url: str):
    database_path = _sqlite_database_path(database_url)
    if database_path is None:
        yield
        return

    lock_path = database_path.with_name(f"{database_path.name}.initialize.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()

        deadline = time.monotonic() + _DATABASE_INITIALIZATION_LOCK_TIMEOUT_SECONDS
        while not _try_lock_file(lock_file):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Timed out waiting for database initialization lock "
                    f"after {_DATABASE_INITIALIZATION_LOCK_TIMEOUT_SECONDS:g} seconds: {lock_path}"
                )
            time.sleep(min(_DATABASE_INITIALIZATION_LOCK_RETRY_SECONDS, remaining))

        try:
            yield
        finally:
            _unlock_file(lock_file)

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
    Base.metadata.create_all(get_engine(database_url))

def initialize_database(database_url: str):
    database_url = normalize_database_url(database_url)
    with _database_initialization_lock(database_url):
        create_schema(database_url)
        from app.db.migrations import upgrade_schema
        upgrade_schema(database_url)

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
