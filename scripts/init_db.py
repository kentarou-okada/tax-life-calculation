"""DB 初期化スクリプト。

- data/ ディレクトリと空の SQLite DB を作成し、全テーブルを定義する。
- 税年度マスタ（2025〜2027）の初期値を投入する（冪等）。

使い方（プロジェクトルートから / PowerShell）:
    .venv/Scripts/python.exe -m scripts.init_db

Alembic による本格的なマイグレーションは、リリース後にスキーマ変更が生じた時点で導入する。
現段階はスキーマ確定前のため、models からの create_all + seed で初期化する。
"""
from __future__ import annotations

from app.config import DATA_DIR, DB_PATH
from app.db import Base, SessionLocal, engine

# create_all にモデルを認識させるため import しておく（副作用 import）
from app import models  # noqa: F401
from app.seeds.living_masters import seed_living_masters
from app.seeds.tax_masters import seed_tax_masters

# create_all は既存テーブルへの列追加を行わないため、後方互換のための軽量マイグレーション。
# (テーブル, 列, 定義) 既存DBに列が無ければ ADD COLUMN する。
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tax_year_input", "housing_loan_deduction_manyen", "REAL NOT NULL DEFAULT 0"),
]


def _apply_column_migrations() -> list[str]:
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                applied.append(f"{table}.{column}")
    return applied


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"DB: {DB_PATH}")
    Base.metadata.create_all(engine)
    print(f"テーブル作成完了: {', '.join(sorted(Base.metadata.tables))}")

    migrated = _apply_column_migrations()
    if migrated:
        print(f"列マイグレーション適用: {', '.join(migrated)}")

    with SessionLocal() as session:
        tax_result = seed_tax_masters(session)
        living_result = seed_living_masters(session)
    print(
        "税年度マスタ投入: "
        f"tax_year_param +{tax_result['params']} 件 / tax_bracket +{tax_result['brackets']} 件"
    )
    print(
        "生活費マスタ投入: "
        f"bank +{living_result['banks']} 件 / category +{living_result['categories']} 件"
    )
    print("初期化が完了しました。")


if __name__ == "__main__":
    main()
