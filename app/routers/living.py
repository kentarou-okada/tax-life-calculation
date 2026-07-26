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
from app.core.living import (
    EntryRow,
    aggregate_by_category,
    aggregate_month,
    aggregate_year,
    round10,
    tax_saving_plan,
)
from app.db import SessionLocal
from app.models import Bank, Category, MonthlyEntry
from app.services.tax_service import result_for_year

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


# 最上位の親名 → 貯蓄グループ。税金貯金 / 積立貯金 の子を分類する。
_SAVING_GROUP_BY_TOP = {"税金貯金": "tax", "積立貯金": "reserve"}


def _saving_group_map(db: Session) -> dict[int, str]:
    """category_id -> 'tax'|'reserve'|''（kind=saving の分類。最上位の親名から決定）。"""
    cats = {c.id: c for c in _all_categories(db)}
    result: dict[int, str] = {}
    for cid, c in cats.items():
        if c.kind != "saving":
            result[cid] = ""
            continue
        top = c
        while top.parent_id is not None and top.parent_id in cats:
            top = cats[top.parent_id]
        result[cid] = _SAVING_GROUP_BY_TOP.get(top.name, "")
    return result


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
    saving_groups = _saving_group_map(db)
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
            saving_group=saving_groups.get(e.category_id, ""),
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
    computed = result_for_year(db, year)
    if computed is None:
        return None
    r, params = computed
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


def _category_annual_rows(db: Session, year: int) -> list[dict]:
    """費目別の年間集計。費目(親)・費目(子)・年間合計の3カラム用の行を返す。

    - 子を持つ親: parent=親名 / child='' / total=小計（親自身＋子の合計） の見出し行 → 各子行 parent='' / child=子名。
    - 子を持たない最上位費目: parent=費目名 / child='' の 1 行。
    """
    totals = aggregate_by_category(_entry_rows(db, year))
    cats = [c for c in _all_categories(db) if c.is_active == 1]
    children_of: dict[Optional[int], list[Category]] = {}
    for c in cats:
        children_of.setdefault(c.parent_id, []).append(c)

    kind_order = {"income": 0, "expense": 1, "saving": 2}
    rows: list[dict] = []
    tops = sorted(children_of.get(None, []), key=lambda c: (kind_order.get(c.kind, 9), c.display_order, c.id))
    for top in tops:
        kids = sorted(children_of.get(top.id, []), key=lambda c: (c.display_order, c.id))
        if kids:
            subtotal = totals.get(top.id, 0) + sum(totals.get(k.id, 0) for k in kids)
            rows.append({"kind": top.kind, "parent": top.name, "child": "", "total": subtotal, "is_group": True})
            for k in kids:
                rows.append({"kind": k.kind, "parent": "", "child": k.name, "total": totals.get(k.id, 0), "is_group": False})
        else:
            rows.append({"kind": top.kind, "parent": top.name, "child": "", "total": totals.get(top.id, 0), "is_group": False})
    return rows


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
        "category_annual": _category_annual_rows(db, year),
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


@router.post("/copy-prev-month", response_class=HTMLResponse)
def living_copy_prev_month(
    request: Request, year: int = Form(...), month: int = Form(...), db: Session = Depends(get_db)
):
    """前月の入力を当月へコピーする（当月の既存セルは上書き）。1月は前年12月から。"""
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_entries = list(
        db.scalars(
            select(MonthlyEntry).where(MonthlyEntry.year == prev_year, MonthlyEntry.month == prev_month)
        )
    )
    copied = 0
    for pe in prev_entries:
        existing = db.scalar(
            select(MonthlyEntry).where(
                MonthlyEntry.bank_id == pe.bank_id,
                MonthlyEntry.category_id == pe.category_id,
                MonthlyEntry.year == year,
                MonthlyEntry.month == month,
            )
        )
        if existing is None:
            db.add(MonthlyEntry(bank_id=pe.bank_id, category_id=pe.category_id, year=year, month=month, amount_yen=pe.amount_yen))
        else:
            existing.amount_yen = pe.amount_yen
        copied += 1
    db.commit()

    ctx = _panel_context(request, db, year, month)
    ctx["notice"] = (
        f"{prev_year}年{prev_month}月の入力を {copied} 件コピーしました。"
        if copied
        else f"{prev_year}年{prev_month}月に入力がありません。"
    )
    return templates.TemplateResponse(request, "living/_month_panel.html", ctx)


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


@router.post("/category/{category_id}/delete", response_class=HTMLResponse)
def delete_category(request: Request, category_id: int, db: Session = Depends(get_db)):
    """費目を削除する。子や実績（月次エントリ）がある場合は削除せず案内する（履歴保護）。"""
    c = db.get(Category, category_id)
    if c is None:
        return _masters_response(request, db, "対象の費目が見つかりません。")
    has_child = db.scalar(select(Category.id).where(Category.parent_id == category_id).limit(1))
    if has_child is not None:
        return _masters_response(request, db, f"「{c.name}」は子費目があるため削除できません（先に子を削除）。")
    has_entry = db.scalar(select(MonthlyEntry.id).where(MonthlyEntry.category_id == category_id).limit(1))
    if has_entry is not None:
        return _masters_response(
            request, db, f"「{c.name}」は入力実績があるため削除できません。「非表示」をご利用ください。"
        )
    name = c.name
    db.delete(c)
    db.commit()
    return _masters_response(request, db, f"費目「{name}」を削除しました。")


@router.post("/category/{category_id}/reparent", response_class=HTMLResponse)
def reparent_category(
    request: Request, category_id: int, parent_id: Optional[int] = Form(None), db: Session = Depends(get_db)
):
    """既存の費目を、選択した最上位費目の子に移動する（または最上位へ戻す）。階層は2段まで。"""
    c = db.get(Category, category_id)
    if c is None:
        return _masters_response(request, db, "対象の費目が見つかりません。")
    if db.scalar(select(Category.id).where(Category.parent_id == category_id).limit(1)) is not None:
        return _masters_response(request, db, f"「{c.name}」は子費目を持つため移動できません（先に子を移動/削除）。")

    new_parent = parent_id or None
    if new_parent is None:
        c.parent_id = None
        notice = f"「{c.name}」を最上位に移動しました。"
    else:
        if new_parent == category_id:
            return _masters_response(request, db, "自分自身は親に指定できません。")
        parent = db.get(Category, new_parent)
        if parent is None:
            return _masters_response(request, db, "移動先の費目が見つかりません。")
        if parent.parent_id is not None:
            return _masters_response(request, db, "移動先は最上位の費目のみ選べます（階層は2段まで）。")
        if db.scalar(select(Category.id).where(Category.parent_id == parent.id, Category.name == c.name, Category.id != c.id).limit(1)) is not None:
            return _masters_response(request, db, f"「{parent.name}」に同名の「{c.name}」が既にあります。")
        c.parent_id = parent.id
        c.kind = parent.kind  # 子は親の種別に合わせる
        notice = f"「{c.name}」を「{parent.name}」の子に移動しました。"

    # 並びは末尾へ
    c.display_order = (db.scalar(select(Category.display_order).order_by(Category.display_order.desc())) or 0) + 1
    db.commit()
    return _masters_response(request, db, notice)


@router.post("/category/{category_id}/move", response_class=HTMLResponse)
def move_category(request: Request, category_id: int, direction: str = Form(...), db: Session = Depends(get_db)):
    """同じ親・同じ並びの中で費目の表示順を上下に入れ替える。"""
    c = db.get(Category, category_id)
    if c is None:
        return _masters_response(request, db, "対象の費目が見つかりません。")
    siblings = list(
        db.scalars(
            select(Category)
            .where(Category.parent_id.is_(None) if c.parent_id is None else Category.parent_id == c.parent_id)
            .order_by(Category.display_order, Category.id)
        )
    )
    idx = next((i for i, s in enumerate(siblings) if s.id == c.id), None)
    swap = idx - 1 if direction == "up" else idx + 1
    if idx is not None and 0 <= swap < len(siblings):
        other = siblings[swap]
        c.display_order, other.display_order = other.display_order, c.display_order
        # display_order が同値だと入れ替わらないため、必要なら再採番
        if c.display_order == other.display_order:
            c.display_order = swap
            other.display_order = idx
        db.commit()
    return _masters_response(request, db, "表示順を更新しました。")
