"""DB の税年度マスタ・入力から税額を算出する橋渡し。純粋関数 app.core.tax に委譲する。

ルータ（画面・エクスポート）から共通利用し、TaxParams/TaxInputs の組み立てを一箇所に集約する。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tax import Bracket, TaxInputs, TaxParams, TaxResult, calculate_tax
from app.models import TaxBracket, TaxYearInput, TaxYearParam


def brackets_for(db: Session, year: int) -> tuple[Bracket, ...]:
    rows = db.scalars(
        select(TaxBracket).where(TaxBracket.year == year).order_by(TaxBracket.lower_bound_manyen)
    )
    return tuple(
        Bracket(lower=r.lower_bound_manyen, upper=r.upper_bound_manyen, rate=r.rate, deduction=r.deduction_manyen)
        for r in rows
    )


def params_from_db(db: Session, param: TaxYearParam) -> TaxParams:
    return TaxParams(
        basic_deduction=param.basic_deduction_manyen,
        blue_return_deduction=param.blue_return_deduction_manyen,
        flat_rate_tax=param.flat_rate_tax_manyen,
        reconstruction_tax_rate=param.reconstruction_tax_rate,
        resident_tax_rate=param.resident_tax_rate,
        resident_tax_deduction=param.resident_tax_deduction_manyen,
        income_tax_rate_mode=param.income_tax_rate_mode,
        income_tax_rate_override=param.income_tax_rate_override,
        income_tax_deduction_override=param.income_tax_deduction_override,
        brackets=brackets_for(db, param.year),
        consumption_tax_method=param.consumption_tax_method,
        consumption_tax_rate=param.consumption_tax_rate,
        furusato_method=param.furusato_method,
        income_tax_split_count=param.income_tax_split_count,
        resident_tax_split_count=param.resident_tax_split_count,
        consumption_tax_split_count=param.consumption_tax_split_count,
    )


def inputs_from_db(ti: TaxYearInput) -> TaxInputs:
    return TaxInputs(
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


def result_for_year(db: Session, year: int) -> Optional[tuple[TaxResult, TaxParams]]:
    """その年の税額結果と使用パラメータを返す。入力が無い/計算不能なら None。"""
    param = db.get(TaxYearParam, year)
    ti = db.get(TaxYearInput, year)
    if param is None or ti is None:
        return None
    params = params_from_db(db, param)
    try:
        result = calculate_tax(params, inputs_from_db(ti))
    except (ValueError, NotImplementedError):
        return None
    return result, params
