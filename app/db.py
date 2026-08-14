"""DB エンジン・セッション・宣言的ベース。

SQLite では外部キー制約が既定で無効なため、接続ごとに PRAGMA foreign_keys=ON を有効化する。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DB_PATH, database_url


class Base(DeclarativeBase):
    """全モデル共通の宣言的ベース。"""


engine = create_engine(database_url(), future=True)

# create_all は既存テーブルへの列追加を行わないため、後方互換の軽量マイグレーション。
# (テーブル, 列, 定義) 既存DBに列が無ければ ADD COLUMN する。新テーブルは create_all が作成。
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tax_year_input", "housing_loan_deduction_manyen", "REAL NOT NULL DEFAULT 0"),
]


def ensure_schema() -> list[str]:
    """不足テーブルを作成し、必要な列マイグレーションを適用する（冪等）。追加した列名を返す。"""
    from app import models  # noqa: F401  モデルを登録するための副作用 import

    if str(DB_PATH) != ":memory:":
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)

    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                applied.append(f"{table}.{column}")
    return applied


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):  # noqa: ANN001
    """SQLite 接続時に外部キー制約を有効化する。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
