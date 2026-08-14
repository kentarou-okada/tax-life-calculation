"""税金算出 画面・API（Jinja2 + HTMX）。

- GET  /tax                … ページ全体
- GET  /tax/panel?year=…   … 年度切替時のパネル（フォーム＋結果）差し替え
- POST /tax/calculate      … 入力変更時の結果フラグメント再計算（永続化しない）
- POST /tax/save           … 入力・年度パラメータを保存し結果フラグメントを返す
- POST /tax/add-year       … 新しい税年度を追加（直近年度の設定を引き継ぐ）してパネルを返す

計算そのものは app.core.tax の純粋関数に委譲し、本モジュールは DB とフォームの橋渡しに徹する。
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

from app.config import BASE_DIR, asset_version
from app.core.tax import Bracket, TaxInputs, TaxParams, TaxResult, calculate_tax
from app.db import SessionLocal
from app.models import TaxBracket, TaxYearInput, TaxYearParam
from app.seeds.tax_masters import STANDARD_BRACKETS

# 追加可能な年度の範囲（現実的なガード）
MIN_YEAR = 2000
MAX_YEAR = 2100

router = APIRouter(prefix="/tax", tags=["tax"])
templates = Jinja2Templates(directory=str(Path(BASE_DIR) / "app" / "templates"))
templates.env.globals["asset_v"] = asset_version()

# 消費税の算定方式（§5-2：将来切替できる余地。ラベルは画面表示用）
CONSUMPTION_METHODS: list[tuple[str, str]] = [
    ("income_5pct", "所得金額 × 5%（簡易見積り）"),
    ("invoice_2wari", "インボイス2割特例（未実装）"),
    ("none", "消費税なし（0）"),
]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# DB 取得ヘルパ
# --------------------------------------------------------------------------- #
def _available_years(db: Session) -> list[int]:
    return list(db.scalars(select(TaxYearParam.year).order_by(TaxYearParam.year)))


def _default_year(years: list[int]) -> Optional[int]:
    if not years:
        return None
    current = _dt.date.today().year
    candidates = [y for y in years if y <= current]
    return max(candidates) if candidates else min(years)


def _create_year(db: Session, year: int, source_year: Optional[int]) -> None:
    """新しい税年度の tax_year_param と累進表を作成する。

    source_year があればその年度の設定・累進表を引き継ぐ（毎年の運用で前年設定を土台にできる）。
    無ければモデル既定値＋標準累進表（REQUIREMENTS.md §4）で作成する。
    """
    src = db.get(TaxYearParam, source_year) if source_year is not None else None
    if src is not None:
        db.add(
            TaxYearParam(
                year=year,
                basic_deduction_manyen=src.basic_deduction_manyen,
                blue_return_deduction_manyen=src.blue_return_deduction_manyen,
                flat_rate_tax_manyen=src.flat_rate_tax_manyen,
                reconstruction_tax_rate=src.reconstruction_tax_rate,
                resident_tax_rate=src.resident_tax_rate,
                resident_tax_deduction_manyen=src.resident_tax_deduction_manyen,
                income_tax_rate_mode=src.income_tax_rate_mode,
                income_tax_rate_override=src.income_tax_rate_override,
                income_tax_deduction_override=src.income_tax_deduction_override,
                consumption_tax_method=src.consumption_tax_method,
                consumption_tax_rate=src.consumption_tax_rate,
                furusato_method=src.furusato_method,
                income_tax_split_count=src.income_tax_split_count,
                resident_tax_split_count=src.resident_tax_split_count,
                consumption_tax_split_count=src.consumption_tax_split_count,
            )
        )
        for b in src.brackets:
            db.add(
                TaxBracket(
                    year=year,
                    lower_bound_manyen=b.lower_bound_manyen,
                    upper_bound_manyen=b.upper_bound_manyen,
                    rate=b.rate,
                    deduction_manyen=b.deduction_manyen,
                )
            )
    else:
        db.add(TaxYearParam(year=year))
        for lower, upper, rate, ded in STANDARD_BRACKETS:
            db.add(
                TaxBracket(
                    year=year,
                    lower_bound_manyen=lower,
                    upper_bound_manyen=upper,
                    rate=rate,
                    deduction_manyen=ded,
                )
            )
    db.commit()


def _brackets_for(db: Session, year: int) -> tuple[Bracket, ...]:
    rows = db.scalars(
        select(TaxBracket).where(TaxBracket.year == year).order_by(TaxBracket.lower_bound_manyen)
    )
    return tuple(
        Bracket(lower=r.lower_bound_manyen, upper=r.upper_bound_manyen, rate=r.rate, deduction=r.deduction_manyen)
        for r in rows
    )


# --------------------------------------------------------------------------- #
# フォーム値の受け皿（POST）。空文字は None/0.0 に正規化する。
# --------------------------------------------------------------------------- #
def _f(value: Optional[str], default: float = 0.0) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _of(value: Optional[str]) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class TaxFormValues:
    """フォーム/DB を共通に扱うためのビュー（テンプレートの value 埋めに使用）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _form_from_db(param: TaxYearParam, tax_input: Optional[TaxYearInput]) -> TaxFormValues:
    i = tax_input
    return TaxFormValues(
        year=param.year,
        # 収入・経費
        business_income=getattr(i, "business_income_manyen", 0.0) if i else 0.0,
        salary_income=getattr(i, "salary_income_manyen", 0.0) if i else 0.0,
        expenses=getattr(i, "expenses_manyen", 0.0) if i else 0.0,
        # 各種控除（入力側）
        spouse_special_deduction=getattr(i, "spouse_special_deduction_manyen", 0.0) if i else 0.0,
        life_insurance_deduction=getattr(i, "life_insurance_deduction_manyen", 0.0) if i else 0.0,
        social_insurance_deduction=getattr(i, "social_insurance_deduction_manyen", 0.0) if i else 0.0,
        small_biz_mutual_aid_deduction=getattr(i, "small_biz_mutual_aid_deduction_manyen", 0.0) if i else 0.0,
        other_income_deduction=getattr(i, "other_income_deduction_manyen", 0.0) if i else 0.0,
        earthquake_insurance_deduction=getattr(i, "earthquake_insurance_deduction_manyen", 0.0) if i else 0.0,
        housing_loan_deduction=getattr(i, "housing_loan_deduction_manyen", 0.0) if i else 0.0,
        donation=getattr(i, "donation_manyen", 0.0) if i else 0.0,
        # 年度パラメータ（マスタ）
        basic_deduction=param.basic_deduction_manyen,
        blue_return_deduction=param.blue_return_deduction_manyen,
        income_tax_rate_mode=param.income_tax_rate_mode,
        income_tax_rate_override=param.income_tax_rate_override,
        income_tax_deduction_override=param.income_tax_deduction_override,
        consumption_tax_method=param.consumption_tax_method,
    )


def _build_params(db: Session, db_param: TaxYearParam, fv: TaxFormValues) -> TaxParams:
    """DB のマスタを土台に、フォームで上書きされた値を反映して TaxParams を組む。"""
    return TaxParams(
        basic_deduction=fv.basic_deduction,
        blue_return_deduction=fv.blue_return_deduction,
        flat_rate_tax=db_param.flat_rate_tax_manyen,
        reconstruction_tax_rate=db_param.reconstruction_tax_rate,
        resident_tax_rate=db_param.resident_tax_rate,
        resident_tax_deduction=db_param.resident_tax_deduction_manyen,
        income_tax_rate_mode=fv.income_tax_rate_mode,
        income_tax_rate_override=fv.income_tax_rate_override,
        income_tax_deduction_override=fv.income_tax_deduction_override,
        brackets=_brackets_for(db, db_param.year),
        consumption_tax_method=fv.consumption_tax_method,
        consumption_tax_rate=db_param.consumption_tax_rate,
        furusato_method=db_param.furusato_method,
        income_tax_split_count=db_param.income_tax_split_count,
        resident_tax_split_count=db_param.resident_tax_split_count,
        consumption_tax_split_count=db_param.consumption_tax_split_count,
    )


def _build_inputs(fv: TaxFormValues) -> TaxInputs:
    return TaxInputs(
        business_income=fv.business_income,
        salary_income=fv.salary_income,
        expenses=fv.expenses,
        spouse_special_deduction=fv.spouse_special_deduction,
        life_insurance_deduction=fv.life_insurance_deduction,
        social_insurance_deduction=fv.social_insurance_deduction,
        small_biz_mutual_aid_deduction=fv.small_biz_mutual_aid_deduction,
        other_income_deduction=fv.other_income_deduction,
        earthquake_insurance_deduction=fv.earthquake_insurance_deduction,
        housing_loan_deduction=fv.housing_loan_deduction,
        donation=fv.donation,
    )


def _compute(db: Session, db_param: TaxYearParam, fv: TaxFormValues) -> tuple[Optional[TaxResult], Optional[str]]:
    """(結果, エラーメッセージ) を返す。未実装方式などは例外を捕捉して文言化する。"""
    try:
        params = _build_params(db, db_param, fv)
        result = calculate_tax(params, _build_inputs(fv))
        return result, None
    except (NotImplementedError, ValueError) as e:
        return None, str(e)


# --------------------------------------------------------------------------- #
# ルート
# --------------------------------------------------------------------------- #
def _panel_context(
    request: Request,
    db: Session,
    year: int,
    fv: TaxFormValues,
    saved: bool = False,
    notice: Optional[str] = None,
) -> dict:
    db_param = db.get(TaxYearParam, year)
    result, error = _compute(db, db_param, fv)
    return {
        "request": request,
        "years": _available_years(db),
        "year": year,
        "fv": fv,
        "result": result,
        "error": error,
        "saved": saved,
        "notice": notice,
        "consumption_methods": CONSUMPTION_METHODS,
    }


@router.get("", response_class=HTMLResponse)
def tax_page(request: Request, year: Optional[int] = None, db: Session = Depends(get_db)):
    years = _available_years(db)
    y = year if (year in years) else _default_year(years)
    if y is None:
        return templates.TemplateResponse(request, "tax/index.html", {"years": [], "no_data": True})
    db_param = db.get(TaxYearParam, y)
    fv = _form_from_db(db_param, db.get(TaxYearInput, y))
    ctx = _panel_context(request, db, y, fv)
    ctx["no_data"] = False
    return templates.TemplateResponse(request, "tax/index.html", ctx)


@router.get("/panel", response_class=HTMLResponse)
def tax_panel(request: Request, year: int, db: Session = Depends(get_db)):
    db_param = db.get(TaxYearParam, year)
    fv = _form_from_db(db_param, db.get(TaxYearInput, year))
    return templates.TemplateResponse(request, "tax/_panel.html", _panel_context(request, db, year, fv))


@router.post("/add-year", response_class=HTMLResponse)
def tax_add_year(
    request: Request, new_year: Optional[int] = Form(default=None), db: Session = Depends(get_db)
):
    """新しい税年度を追加してそのパネルを返す。範囲外・重複は既定年度へフォールバックし通知する。"""
    years = _available_years(db)

    if new_year is None or not (MIN_YEAR <= new_year <= MAX_YEAR):
        target = _default_year(years)
        notice = f"{MIN_YEAR}〜{MAX_YEAR} の範囲で年度を入力してください。"
    elif new_year in years:
        target = new_year
        notice = f"{new_year} 年はすでに存在します。切り替えました。"
    else:
        _create_year(db, new_year, source_year=max(years) if years else None)
        target = new_year
        src = max(years) if years else None
        notice = (
            f"{new_year} 年を追加しました（{src} 年の設定を引き継ぎ）。"
            if src is not None
            else f"{new_year} 年を追加しました。"
        )

    db_param = db.get(TaxYearParam, target)
    fv = _form_from_db(db_param, db.get(TaxYearInput, target))
    return templates.TemplateResponse(
        request, "tax/_panel.html", _panel_context(request, db, target, fv, notice=notice)
    )


def _fv_from_form(form) -> TaxFormValues:
    return TaxFormValues(
        year=int(_f(form.get("year"))),
        business_income=_f(form.get("business_income")),
        salary_income=_f(form.get("salary_income")),
        expenses=_f(form.get("expenses")),
        spouse_special_deduction=_f(form.get("spouse_special_deduction")),
        life_insurance_deduction=_f(form.get("life_insurance_deduction")),
        social_insurance_deduction=_f(form.get("social_insurance_deduction")),
        small_biz_mutual_aid_deduction=_f(form.get("small_biz_mutual_aid_deduction")),
        other_income_deduction=_f(form.get("other_income_deduction")),
        earthquake_insurance_deduction=_f(form.get("earthquake_insurance_deduction")),
        housing_loan_deduction=_f(form.get("housing_loan_deduction")),
        donation=_f(form.get("donation")),
        basic_deduction=_f(form.get("basic_deduction")),
        blue_return_deduction=_f(form.get("blue_return_deduction")),
        income_tax_rate_mode=(form.get("income_tax_rate_mode") or "auto"),
        income_tax_rate_override=_of(form.get("income_tax_rate_override")),
        income_tax_deduction_override=_of(form.get("income_tax_deduction_override")),
        consumption_tax_method=(form.get("consumption_tax_method") or "income_5pct"),
    )


@router.post("/calculate", response_class=HTMLResponse)
async def tax_calculate(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fv = _fv_from_form(form)
    db_param = db.get(TaxYearParam, fv.year)
    result, error = _compute(db, db_param, fv)
    return templates.TemplateResponse(
        request,
        "tax/_result.html",
        {"year": fv.year, "fv": fv, "result": result, "error": error, "saved": False},
    )


@router.post("/save", response_class=HTMLResponse)
async def tax_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    fv = _fv_from_form(form)
    year = fv.year

    # 年度パラメータ（マスタ）の編集可能項目を保存
    db_param = db.get(TaxYearParam, year)
    db_param.basic_deduction_manyen = fv.basic_deduction
    db_param.blue_return_deduction_manyen = fv.blue_return_deduction
    db_param.income_tax_rate_mode = fv.income_tax_rate_mode
    db_param.income_tax_rate_override = fv.income_tax_rate_override
    db_param.income_tax_deduction_override = fv.income_tax_deduction_override
    db_param.consumption_tax_method = fv.consumption_tax_method

    # 税年度入力（実データ）を upsert
    ti = db.get(TaxYearInput, year)
    if ti is None:
        ti = TaxYearInput(year=year)
        db.add(ti)
    ti.business_income_manyen = fv.business_income
    ti.salary_income_manyen = fv.salary_income
    ti.expenses_manyen = fv.expenses
    ti.spouse_special_deduction_manyen = fv.spouse_special_deduction
    ti.life_insurance_deduction_manyen = fv.life_insurance_deduction
    ti.social_insurance_deduction_manyen = fv.social_insurance_deduction
    ti.small_biz_mutual_aid_deduction_manyen = fv.small_biz_mutual_aid_deduction
    ti.other_income_deduction_manyen = fv.other_income_deduction
    ti.earthquake_insurance_deduction_manyen = fv.earthquake_insurance_deduction
    ti.housing_loan_deduction_manyen = fv.housing_loan_deduction
    ti.donation_manyen = fv.donation
    db.commit()

    result, error = _compute(db, db_param, fv)
    return templates.TemplateResponse(
        request,
        "tax/_result.html",
        {"year": year, "fv": fv, "result": result, "error": error, "saved": True},
    )


@router.post("/copy-prev-year", response_class=HTMLResponse)
def tax_copy_prev_year(request: Request, year: int = Form(...), db: Session = Depends(get_db)):
    """前年の税年度入力を当年フォームに流用する（保存はせず、確認後に保存ボタンで確定）。"""
    prev = db.get(TaxYearInput, year - 1)
    db_param = db.get(TaxYearParam, year)
    if prev is None:
        fv = _form_from_db(db_param, db.get(TaxYearInput, year))
        notice = f"{year - 1} 年の入力がありません（先に {year - 1} 年を保存してください）。"
    else:
        # 前年の入力値を当年の year で写した fv を作る（マスタは当年のものを使用）
        fv = _form_from_db(db_param, prev)
        fv.year = year
        notice = f"{year - 1} 年の入力を読み込みました。内容を確認して「保存」してください。"
    return templates.TemplateResponse(
        request, "tax/_panel.html", _panel_context(request, db, year, fv, notice=notice)
    )
