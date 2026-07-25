"""DB エンジン・セッション・宣言的ベース。

SQLite では外部キー制約が既定で無効なため、接続ごとに PRAGMA foreign_keys=ON を有効化する。
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import database_url


class Base(DeclarativeBase):
    """全モデル共通の宣言的ベース。"""


engine = create_engine(database_url(), future=True)


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):  # noqa: ANN001
    """SQLite 接続時に外部キー制約を有効化する。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
