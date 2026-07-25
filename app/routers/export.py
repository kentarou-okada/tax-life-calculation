"""CSV / Excel エクスポートと SQLite 手動バックアップ。

- GET  /export/living.csv?year=   月次収支（明細＋サマリ）CSV
- GET  /export/living.xlsx?year=  月次収支 Excel（明細/サマリ 2シート）
- GET  /export/tax.csv?year=      税金算出結果 CSV（year 省略で全年度）
- GET  /export/tax.xlsx?year=     税金算出結果 Excel
- POST /export/backup             data/app.db を data/backups/ にタイムスタンプ付きで複製

CSV は Excel で文字化けしないよう UTF-8 BOM 付き。金額は生活費=円 / 税金=万円。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DATA_DIR, DB_PATH
from app.core.living import aggregate_year
from app.core.living import EntryRow
from app.db import SessionLocal
from app.models import Bank, Category, MonthlyEntry, TaxYearParam
from app.services.tax_service import result_for_year

router = APIRouter(prefix="/export", tags=["export"])

CSV_BOM = "﻿"

TAX_HEADERS = [
    "年度", "所得金額", "課税所得金額", "適用税率", "控除額", "所得税額", "復興特別所得税",
    "住民税", "市県民税(均等割)", "消費税", "税金合計", "ふるさと納税上限額",
    "手取り合計", "各種支払後残金", "所得税分割", "住民税分割", "消費税分割",
]
LIVING_DETAIL_HEADERS = ["年", "月", "銀行", "費目(親)", "費目", "種別", "金額(円)"]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# データ収集
# --------------------------------------------------------------------------- #
def _living_detail_rows(db: Session, year: int) -> list[list]:
    cats = {c.id: c for c in db.scalars(select(Category))}
    banks = {b.id: b.name for b in db.scalars(select(Bank))}
    kind_label = {"income": "収入", "expense": "支払", "saving": "積立・税金貯金"}
    rows: list[list] = []
    entries = db.scalars(
        select(MonthlyEntry).where(MonthlyEntry.year == year).order_by(MonthlyEntry.month, MonthlyEntry.bank_id)
    )
    for e in entries:
        c = cats.get(e.category_id)
        parent = cats.get(c.parent_id).name if (c and c.parent_id and cats.get(c.parent_id)) else ""
        rows.append([
            year, e.month, banks.get(e.bank_id, str(e.bank_id)),
            parent, c.name if c else str(e.category_id),
            kind_label.get(c.kind, c.kind) if c else "", e.amount_yen,
        ])
    return rows


def _living_summary(db: Session, year: int):
    banks = list(db.scalars(select(Bank).order_by(Bank.display_order, Bank.id)))
    kinds = {c.id: c.kind for c in db.scalars(select(Category))}
    rows = [
        EntryRow(bank_id=e.bank_id, category_id=e.category_id, kind=kinds.get(e.category_id, "expense"),
                 month=e.month, amount_yen=e.amount_yen)
        for e in db.scalars(select(MonthlyEntry).where(MonthlyEntry.year == year))
    ]
    ya = aggregate_year(rows)
    headers = ["月", "収入", "支払", "余剰金", "積立・税金貯金"] + [f"{b.name}(支払)" for b in banks]
    body: list[list] = []
    for m in range(1, 13):
        ma = ya.months[m]
        body.append([m, ma.income, ma.payment, ma.surplus, ma.saving]
                     + [ma.by_bank_outflow.get(b.id, 0) for b in banks])
    t = ya.total
    body.append(["年計", t.income, t.payment, t.surplus, t.saving]
                + [t.by_bank_outflow.get(b.id, 0) for b in banks])
    return headers, body


def _tax_rows(db: Session, year: Optional[int]) -> list[list]:
    years = [year] if year is not None else list(
        db.scalars(select(TaxYearParam.year).order_by(TaxYearParam.year))
    )
    rows: list[list] = []
    for y in years:
        computed = result_for_year(db, y)
        if computed is None:
            continue
        r, params = computed
        rows.append([
            y, r.income, r.taxable_income, r.income_tax_rate, r.income_tax_deduction, r.income_tax,
            r.reconstruction_tax, r.resident_tax, r.flat_rate_tax, r.consumption_tax, r.total_tax,
            r.furusato_limit, r.net_income, r.remaining,
            r.income_tax_monthly, r.resident_tax_monthly, r.consumption_tax_monthly,
        ])
    return rows


# --------------------------------------------------------------------------- #
# 出力ヘルパ
# --------------------------------------------------------------------------- #
def _csv_response(filename: str, sections: list[tuple[Optional[str], list, list[list]]]) -> Response:
    buf = io.StringIO()
    buf.write(CSV_BOM)
    writer = csv.writer(buf)
    for i, (title, headers, body) in enumerate(sections):
        if i > 0:
            writer.writerow([])
        if title:
            writer.writerow([title])
        if headers:
            writer.writerow(headers)
        writer.writerows(body)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _xlsx_response(filename: str, sheets: list[tuple[str, list, list[list]]]) -> Response:
    wb = Workbook()
    wb.remove(wb.active)
    for name, headers, body in sheets:
        ws = wb.create_sheet(title=name)
        if headers:
            ws.append(headers)
        for row in body:
            ws.append(row)
    bio = io.BytesIO()
    wb.save(bio)
    return Response(
        content=bio.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# ルート：エクスポート
# --------------------------------------------------------------------------- #
@router.get("/living.csv")
def living_csv(year: int, db: Session = Depends(get_db)):
    headers, summary = _living_summary(db, year)
    return _csv_response(
        f"living_{year}.csv",
        [
            ("月次明細", LIVING_DETAIL_HEADERS, _living_detail_rows(db, year)),
            (f"{year}年 月次サマリ", headers, summary),
        ],
    )


@router.get("/living.xlsx")
def living_xlsx(year: int, db: Session = Depends(get_db)):
    headers, summary = _living_summary(db, year)
    return _xlsx_response(
        f"living_{year}.xlsx",
        [
            ("月次明細", LIVING_DETAIL_HEADERS, _living_detail_rows(db, year)),
            ("月次サマリ", headers, summary),
        ],
    )


@router.get("/tax.csv")
def tax_csv(year: Optional[int] = None, db: Session = Depends(get_db)):
    name = f"tax_{year}.csv" if year is not None else "tax_all.csv"
    return _csv_response(name, [("税金算出結果(万円)", TAX_HEADERS, _tax_rows(db, year))])


@router.get("/tax.xlsx")
def tax_xlsx(year: Optional[int] = None, db: Session = Depends(get_db)):
    name = f"tax_{year}.xlsx" if year is not None else "tax_all.xlsx"
    return _xlsx_response(name, [("税金算出結果", TAX_HEADERS, _tax_rows(db, year))])


# --------------------------------------------------------------------------- #
# ルート：バックアップ
# --------------------------------------------------------------------------- #
@router.post("/backup", response_class=HTMLResponse)
def backup(request: Request):
    """SQLite の一貫性を保つオンラインバックアップ API で data/backups/ に複製する。"""
    if not Path(DB_PATH).exists():
        return HTMLResponse('<span class="backup-msg err">DB ファイルが見つかりません。</span>')
    backups_dir = DATA_DIR / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"app_{stamp}.db"

    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size_kb = dest.stat().st_size / 1024
    return HTMLResponse(
        f'<span class="backup-msg ok">✔ バックアップを作成しました：'
        f'data/backups/{dest.name}（{size_kb:.0f} KB）</span>'
    )
