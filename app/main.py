"""FastAPI エントリポイント。ローカル単一ユーザー専用（127.0.0.1・認証なし）。

起動:
    .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR
from app.routers import export as export_router
from app.routers import living as living_router
from app.routers import summary as summary_router
from app.routers import tax as tax_router

app = FastAPI(title="家計・税金管理")

app.mount(
    "/static",
    StaticFiles(directory=str(Path(BASE_DIR) / "app" / "static")),
    name="static",
)

app.include_router(tax_router.router)
app.include_router(living_router.router)
app.include_router(summary_router.router)
app.include_router(export_router.router)


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/tax")
