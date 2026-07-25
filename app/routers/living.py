"""生活費収支 画面・API（Jinja2 + HTMX）。REQUIREMENTS.md §3。

- GET  /living                     … ページ全体
- GET  /living/panel?year=&month=  … 年度パネル（税金貯金目安＋年次サマリ＋月エディタ）差し替え
- GET  /living/month?year=&month=  … 月エディタ（グリッド＋月次集計）差し替え
- POST /living/entry               … 1 セルの金額を upsert（10円単位）→ 月次集計＋年次サマリ(OOB)更新
- GET  /living/masters             … 銀行・費目カテゴリ管理
- POST /living/bank …/ /living/category … マスタの追加・改名・有効切替

集計は app.core.living の純粋関数に委譲する。金額は円（10円単位）。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.core.living import EntryRow, aggregate_month, aggregate_year, round10, tax_saving_plan
from app.core.tax import Bracket, TaxInputs, TaxParams, calculate_tax
from app.db import SessionLocal
from app.models import Bank, Category, MonthlyEntry, TaxBracket, TaxYearInput, TaxYearParam

router = APIRouter(prefix="/living", tags=["living"])
templates = Jinja2Templates(directory=str(Path(BASE_DIR) / "app" / "templates"))

KIND_LABELS = {"income": "収入", "expense": "支払", "saving": "積立・税金貯金"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# マスタ取得
# --------------------------------------------------------------------------- #
def _active_banks(db: Session) -> list[Bank]:
    return list(db.scalars(select(Bank).where(Bank.is_active == 1).order_by(Bank.display_order, Bank.id)))


def _all_banks(db: Session) -> list[Bank]:
    return list(db.scalars(select(Bank).order_by(Bank.display_order, Bank.id)))


def _all_categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.display_order, Category.id)))


def _category_kind_map(db: Session) -> dict[int, str]:
    return {c.id: c.kind for c in _all_categories(db)}


def _grid_rows(db: Session) -> list[dict]:
    """入力グリッドの行構造。子を持つカテゴリはヘッダ、葉カテゴリは入力行。"""
    cats = [c for c in _all_categories(db) if c.is_active == 1]
    children_of: dict[Optional[int], list[Category]] = {}
    for c in cats:
        children_of.setdefault(c.parent_id, []).append(c)

    rows: list[dict] = []
    kind_order = {"income": 0, "expense": 1, "saving": 2}
    tops = sorted(children_of.get(None, []), key=lambda c: (kind_order.get(c.kind, 9), c.display_order, c.id))
    for top in tops:
        kids = children_of.get(top.id, [])
        if kids:
            rows.append({"type": "header", "category": top})
            for k in sorted(kids, key=lambda c: (c.display_order, c.id)):
                rows.append({"type": "input", "category": k})
        else:
            rows.append({"type": "input", "category": top})
    return rows


# --------------------------------------------------------------------------- #
# エントリ取得・集計
# --------------------------------------------------------------------------- #
def _entry_rows(db: Session, year: int, month: Optional[int] = None) -> list[EntryRow]:
    kinds = _category_kind_map(db)
    stmt = select(MonthlyEntry).where(MonthlyEntry.year == year)
    if month is not None:
        stmt = stmt.where(MonthlyEntry.month == month)
    return [
        EntryRow(
            bank_id=e.bank_id,
            category_id=e.category_id,
            kind=kinds.get(e.category_id, "expense"),
            month=e.month,
            amount_yen=e.amount_yen,
        )
        for e in db.scalars(stmt)
    ]


def _month_entry_map(db: Session, year: int, month: int) -> dict[str, int]:
    """'bank_id:category_id' -> amount_yen（当月）。グリッドの値埋めに使う（Jinja で扱いやすい文字列キー）。"""
    result: dict[str, int] = {}
    for e in db.scalars(
        select(MonthlyEntry).where(MonthlyEntry.year == year, MonthlyEntry.month == month)
    ):
        result[f"{e.bank_id}:{e.category_id}"] = e.amount_yen
    return result


# --------------------------------------------------------------------------- #
# 税金貯金目安（§4 分割式と連動）
# --------------------------------------------------------------------------- #
def _tax_saving_plan(db: Session, year: int):
    param = db.get(TaxYearParam, year)
    ti = db.get(TaxYearInput, year)
    if param is None or ti is None:
        return None
    brackets = tuple(
        Bracket(lower=r.lower_bound_manyen, upper=r.upper_bound_manyen, rate=r.rate, deduction=r.deduction_manyen)
        for r in db.scalars(
            select(TaxBracket).where(TaxBracket.year == year).order_by(TaxBracket.lower_bound_manyen)
        )
    )
    params = TaxParams(
        basic_deduction=param.basic_deduction_manyen,
        blue_return_deduction=param.blue_return_deduction_manyen,
        flat_rate_tax=param.flat_rate_tax_manyen,
        reconstruction_tax_rate=param.reconstruction_tax_rate,
        resident_tax_rate=param.resident_tax_rate,
        resident_tax_deduction=param.resident_tax_deduction_manyen,
        income_tax_rate_mode=param.income_tax_rate_mode,
        income_tax_rate_override=param.income_tax_rate_override,
        income_tax_deduction_override=param.income_tax_deduction_override,
        brackets=brackets,
        consumption_tax_method=param.consumption_tax_method,
        consumption_tax_rate=param.consumption_tax_rate,
        furusato_method=param.furusato_method,
        income_tax_split_count=param.income_tax_split_count,
        resident_tax_split_count=param.resident_tax_split_count,
        consumption_tax_split_count=param.consumption_tax_split_count,
    )
    inputs = TaxInputs(
        business_income=ti.business_income_manyen,
        salary_income=ti.salary_income_manyen,
        expenses=ti.expenses_manyen,
        spouse_special_deduction=ti.spouse_special_deduction_manyen,
        life_insurance_deduction=ti.life_insurance_deduction_manyen,
        social_insurance_deduction=ti.social_insurance_deduction_manyen,
        small_biz_mutual_aid_deduction=ti.small_biz_mutual_aid_deduction_manyen,
        other_income_deduction=ti.other_income_deduction_manyen,
        earthquake_insurance_deduction=ti.earthquake_insurance_deduction_manyen,
        donation=ti.donation_manyen,
    )
    try:
        r = calculate_tax(params, inputs)
    except (ValueError, NotImplementedError):
        return None
    return tax_saving_plan(
        income_tax_manyen=r.income_tax,
        resident_tax_manyen=r.resident_tax,
        consumption_tax_manyen=r.consumption_tax,
        total_tax_manyen=r.total_tax,
        income_tax_split_count=params.income_tax_split_count,
        resident_tax_split_count=params.resident_tax_split_count,
        consumption_tax_split_count=params.consumption_tax_split_count,
    )


# --------------------------------------------------------------------------- #
# コンテキスト構築
# --------------------------------------------------------------------------- #
def _default_month(year: int) -> int:
    today = _dt.date.today()
    return today.month if year == today.year else 1


def _bank_name_map(db: Session) -> dict[int, str]:
    return {b.id: b.name for b in _all_banks(db)}


def _panel_context(request: Request, db: Session, year: int, month: int) -> dict:
    banks = _active_banks(db)
    year_agg = aggregate_year(_entry_rows(db, year))
    month_agg = aggregate_month(_entry_rows(db, year, month))
    return {
        "request": request,
        "year": year,
        "month": month,
        "months": list(range(1, 13)),
        "banks": banks,
        "bank_names": _bank_name_map(db),
        "grid_rows": _grid_rows(db),
        "entry_map": _month_entry_map(db, year, month),
        "year_agg": year_agg,
        "month_agg": month_agg,
        "plan": _tax_saving_plan(db, year),
    }


# --------------------------------------------------------------------------- #
# ルート：表示
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def living_page(request: Request, year: Optional[int] = None, month: Optional[int] = None, db: Session = Depends(get_db)):
    y = year or _dt.date.today().year
    m = month or _default_month(y)
    ctx = _panel_context(request, db, y, m)
    return templates.TemplateResponse(request, "living/index.html", ctx)


@router.get("/panel", response_class=HTMLResponse)
def living_panel(request: Request, year: int, month: Optional[int] = None, db: Session = Depends(get_db)):
    m = month or _default_month(year)
    return templates.TemplateResponse(request, "living/_panel.html", _panel_context(request, db, year, m))


@router.get("/month", response_class=HTMLResponse)
def living_month(request: Request, year: int, month: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "living/_month_panel.html", _panel_context(request, db, year, month))


# --------------------------------------------------------------------------- #
# ルート：エントリ upsert
# --------------------------------------------------------------------------- #
@router.post("/entry", response_class=HTMLResponse)
def living_entry(
    request: Request,
    bank_id: int = Form(...),
    category_id: int = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    amount_yen: float = Form(0),
    db: Session = Depends(get_db),
):
    amount = max(0, round10(amount_yen))  # 10円単位・非負に正規化
    entry = db.scalar(
        select(MonthlyEntry).where(
            MonthlyEntry.bank_id == bank_id,
            MonthlyEntry.category_id == category_id,
            MonthlyEntry.year == year,
            MonthlyEntry.month == month,
        )
    )
    if amount == 0:
        if entry is not None:
            db.delete(entry)  # 0 は行を残さない
    elif entry is None:
        db.add(MonthlyEntry(bank_id=bank_id, category_id=category_id, year=year, month=month, amount_yen=amount))
    else:
        entry.amount_yen = amount
    db.commit()

    ctx = _panel_context(request, db, year, month)
    return templates.TemplateResponse(request, "living/_entry_response.html", ctx)


# --------------------------------------------------------------------------- #
# ルート：マスタ管理（銀行・カテゴリ）
# --------------------------------------------------------------------------- #
@router.get("/masters", response_class=HTMLResponse)
def masters_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "living/masters.html", _masters_context(request, db))


def _masters_context(request: Request, db: Session, notice: Optional[str] = None) -> dict:
    return {
        "request": request,
        "banks": _all_banks(db),
        "categories": _all_categories(db),
        "top_categories": [c for c in _all_categories(db) if c.parent_id is None],
        "kind_labels": KIND_LABELS,
        "notice": notice,
    }


def _masters_response(request: Request, db: Session, notice: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "living/_masters_body.html", _masters_context(request, db, notice))


@router.post("/bank", response_class=HTMLResponse)
def add_bank(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return _masters_response(request, db, "銀行名を入力してください。")
    if db.scalar(select(Bank.id).where(Bank.name == name)) is not None:
        return _masters_response(request, db, f"「{name}」はすでに存在します。")
    order = (db.scalar(select(Bank.display_order).order_by(Bank.display_order.desc())) or 0) + 1
    db.add(Bank(name=name, display_order=order))
    db.commit()
    return _masters_response(request, db, f"銀行「{name}」を追加しました。")


@router.post("/bank/{bank_id}/rename", response_class=HTMLResponse)
def rename_bank(request: Request, bank_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    b = db.get(Bank, bank_id)
    if b is not None and name.strip():
        b.name = name.strip()
        db.commit()
    return _masters_response(request, db, "銀行名を更新しました。")


@router.post("/bank/{bank_id}/toggle", response_class=HTMLResponse)
def toggle_bank(request: Request, bank_id: int, db: Session = Depends(get_db)):
    b = db.get(Bank, bank_id)
    if b is not None:
        b.is_active = 0 if b.is_active else 1
        db.commit()
    return _masters_response(request, db, "銀行の表示状態を更新しました。")


@router.post("/category", response_class=HTMLResponse)
def add_category(
    request: Request,
    name: str = Form(...),
    kind: str = Form("expense"),
    parent_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return _masters_response(request, db, "費目名を入力してください。")
    pid = parent_id if parent_id else None
    if pid is not None:
        parent = db.get(Category, pid)
        if parent is not None:
            kind = parent.kind  # 子は親の種別に合わせる
    if db.scalar(select(Category.id).where(Category.parent_id.is_(pid) if pid is None else Category.parent_id == pid, Category.name == name)) is not None:
        return _masters_response(request, db, f"「{name}」はすでに存在します。")
    order = (db.scalar(select(Category.display_order).order_by(Category.display_order.desc())) or 0) + 1
    db.add(Category(name=name, kind=kind, parent_id=pid, display_order=order))
    db.commit()
    return _masters_response(request, db, f"費目「{name}」を追加しました。")


@router.post("/category/{category_id}/rename", response_class=HTMLResponse)
def rename_category(request: Request, category_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    c = db.get(Category, category_id)
    if c is not None and name.strip():
        c.name = name.strip()
        db.commit()
    return _masters_response(request, db, "費目名を更新しました。")


@router.post("/category/{category_id}/toggle", response_class=HTMLResponse)
def toggle_category(request: Request, category_id: int, db: Session = Depends(get_db)):
    c = db.get(Category, category_id)
    if c is not None:
        c.is_active = 0 if c.is_active else 1
        db.commit()
    return _masters_response(request, db, "費目の表示状態を更新しました。")
