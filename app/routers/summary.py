"""サマリ画面。生活費の年サマリ・銀行別年間内訳・費目別年間集計・税金貯金の目安をまとめて表示する。

- GET /summary          … ページ全体
- GET /summary/panel?year=… … 年度切替時のパネル差し替え

集計の組み立ては app.routers.living.summary_context に委譲する。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.db import SessionLocal
from app.routers.living import summary_context

router = APIRouter(tags=["summary"])
templates = Jinja2Templates(directory=str(Path(BASE_DIR) / "app" / "templates"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/summary", response_class=HTMLResponse)
def summary_page(request: Request, year: Optional[int] = None, db: Session = Depends(get_db)):
    y = year or _dt.date.today().year
    return templates.TemplateResponse(request, "summary/index.html", summary_context(request, db, y))


@router.get("/summary/panel", response_class=HTMLResponse)
def summary_panel(request: Request, year: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "summary/_panel.html", summary_context(request, db, year))
