from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

from sqlalchemy import create_engine, text, event

from app.core.config import settings

# 判断是否使用 SQLite
_is_sqlite = settings.database_url.startswith("sqlite")

# SQLite 需要绝对路径
if _is_sqlite:
    db_path = settings.database_url.replace("sqlite:///", "")
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(db_path)
    settings.database_url = f"sqlite:///{db_path}"

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

# SQLite 优化设置
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@contextmanager
def get_conn(begin: bool = False):
    if begin:
        with engine.begin() as conn:
            yield conn
    else:
        with engine.connect() as conn:
            yield conn


def fetch_all(sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]


def execute(sql: str, params: Optional[Dict[str, Any]] = None):
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})


def is_sqlite() -> bool:
    return _is_sqlite
