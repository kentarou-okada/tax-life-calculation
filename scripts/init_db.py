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
from app.seeds.tax_masters import seed_tax_masters


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"DB: {DB_PATH}")
    Base.metadata.create_all(engine)
    print(f"テーブル作成完了: {', '.join(sorted(Base.metadata.tables))}")

    with SessionLocal() as session:
        result = seed_tax_masters(session)
    print(
        "税年度マスタ投入: "
        f"tax_year_param +{result['params']} 件 / tax_bracket +{result['brackets']} 件"
    )
    print("初期化が完了しました。")


if __name__ == "__main__":
    main()
