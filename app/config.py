"""アプリ設定。DB パス等の環境依存値を一元管理する。"""
from __future__ import annotations

import os
from pathlib import Path

# プロジェクトルート（app/ の 1 つ上）
BASE_DIR = Path(__file__).resolve().parent.parent

# 実データ置き場。DB ファイルはここに置き、.gitignore で追跡除外する。
DATA_DIR = BASE_DIR / "data"

# SQLite DB ファイル。環境変数 APP_DB_PATH で上書き可能（テストで :memory: 等を差す用途）。
DB_PATH = Path(os.environ.get("APP_DB_PATH", DATA_DIR / "app.db"))


def database_url() -> str:
    """SQLAlchemy 用の接続 URL を返す。"""
    if str(DB_PATH) == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    return f"sqlite+pysqlite:///{DB_PATH.as_posix()}"
