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
from app.db import Base, SessionLocal, ensure_schema
from app.seeds.living_masters import seed_living_masters
from app.seeds.tax_masters import seed_tax_masters


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"DB: {DB_PATH}")
    migrated = ensure_schema()
    print(f"テーブル作成完了: {', '.join(sorted(Base.metadata.tables))}")
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
