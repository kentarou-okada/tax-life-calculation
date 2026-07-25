"""税額算出ロジックの検証。

正: REQUIREMENTS.md §4 検証表 と sample-file.xlsx「税金算出」シート。
"""
from __future__ import annotations

import pytest

from app.core.tax import (
    Bracket,
    TaxInputs,
    TaxParams,
    calculate_tax,
    resolve_income_tax_rate,
    select_bracket,
)
from app.seeds.tax_masters import STANDARD_BRACKETS

# 標準累進表を core の Bracket に変換（seed と同一ソース = REQUIREMENTS.md §4）
STD_BRACKETS: tuple[Bracket, ...] = tuple(
    Bracket(lower=lo, upper=up, rate=rate, deduction=ded) for (lo, up, rate, ded) in STANDARD_BRACKETS
)

# §4 の 2025 入力例（単位=万円）
INPUTS_2025 = TaxInputs(
    business_income=1050.0,   # 事業所得
    salary_income=0.0,        # 給与所得
    expenses=10.0,            # 経費
    spouse_special_deduction=0.0,
    life_insurance_deduction=7.2,
    social_insurance_deduction=76.8,
    small_biz_mutual_aid_deduction=84.0,
    other_income_deduction=0.0,
    earthquake_insurance_deduction=1.3,
    donation=12.0,            # 寄付金（ふるさと納税）
)

# 2025 パラメータ：所得税率は手動上書き（20% / 控除60万）＝Excel N9/O9
PARAMS_2025_MANUAL = TaxParams(
    basic_deduction=58.0,
    blue_return_deduction=65.0,
    reconstruction_tax_rate=0.021,
    resident_tax_rate=0.10,
    income_tax_rate_mode="manual",
    income_tax_rate_override=0.20,
    income_tax_deduction_override=60.0,
    brackets=STD_BRACKETS,
)


@pytest.fixture
def result_2025():
    return calculate_tax(PARAMS_2025_MANUAL, INPUTS_2025)


def test_pretax_base_matches_section4(result_2025):
    """§4 の骨格（所得・課税所得・控除前所得税額）は不変。控除前 = 89.54。"""
    r = result_2025
    assert round(r.income, 2) == 975.0                     # 所得金額
    assert round(r.taxable_income, 2) == 747.7            # 課税所得金額
    assert round(r.income_tax_before_credits, 2) == 89.54  # 所得税額（税額控除前）


def test_verification_table_2025_with_credits(result_2025):
    """住宅ローン(0)・ふるさと納税(12万)の税額控除を反映した結果。

    ふるさと base = 12 − 0.2 = 11.8万 → 所得税分 11.8×0.20=2.36 / 住民税分 11.8×0.80=9.44。
    """
    r = result_2025
    assert round(r.furusato_income_tax_credit, 2) == 2.36
    assert round(r.furusato_resident_credit, 2) == 9.44
    assert r.housing_loan_credit == 0.0
    assert round(r.income_tax, 2) == 87.18        # 89.54 − 2.36
    assert round(r.reconstruction_tax, 2) == 1.83  # 87.18 × 0.021
    assert round(r.resident_tax, 2) == 65.33      # 74.77 − 9.44
    assert round(r.consumption_tax, 2) == 48.75
    assert round(r.total_tax, 2) == 203.59        # 87.18+1.83+65.33+0.5+48.75
    assert round(r.net_income, 2) == 836.41       # 事業1050 − 経費10 − 税203.59
    assert round(r.remaining, 2) == 759.61        # 836.41 − 社会保険76.8
    assert round(r.furusato_limit, 2) == 21.36


def test_income_tax_uses_manual_override_not_auto(result_2025):
    """§5-1: 課税所得747.7 は標準表なら23%区分だが、手動上書き20%/60万で控除前89.54になること。"""
    assert round(result_2025.income_tax_rate, 2) == 0.20
    assert round(result_2025.income_tax_before_credits, 2) == 89.54

    params_auto = TaxParams(
        basic_deduction=58.0,
        blue_return_deduction=65.0,
        income_tax_rate_mode="auto",
        brackets=STD_BRACKETS,
    )
    r_auto = calculate_tax(params_auto, INPUTS_2025)
    assert r_auto.income_tax_rate == 0.23                 # 695〜900万 区分
    assert round(r_auto.income_tax_before_credits, 2) == 108.37  # 747.7*0.23-63.6
    assert round(r_auto.income_tax_before_credits, 2) != round(result_2025.income_tax_before_credits, 2)


def test_housing_loan_credit_reduces_income_tax():
    """住宅ローン控除は所得税額から直接差し引かれ、0 の可能性もある。"""
    from dataclasses import replace

    inputs = replace(INPUTS_2025, donation=0.0, housing_loan_deduction=18.0)
    r = calculate_tax(PARAMS_2025_MANUAL, inputs)
    assert r.housing_loan_credit == 18.0
    assert round(r.income_tax, 2) == round(89.54 - 18.0, 2)  # 71.54
    # 0 なら控除前と一致
    r0 = calculate_tax(PARAMS_2025_MANUAL, replace(INPUTS_2025, donation=0.0, housing_loan_deduction=0.0))
    assert round(r0.income_tax, 2) == 89.54


def test_auto_judgment_selects_23pct_for_747(result_2025):
    """課税所得747.7 の自動判定が 695〜900万（23%/控除63.6）区分を選ぶこと。"""
    rate, deduction = resolve_income_tax_rate(
        TaxParams(basic_deduction=58.0, blue_return_deduction=65.0, brackets=STD_BRACKETS),
        747.7,
    )
    assert rate == 0.23
    assert deduction == 63.6


def test_monthly_splits_2025(result_2025):
    """税額の毎月分割は控除後の税額を分割数で割る。"""
    r = result_2025
    assert r.income_tax_monthly == pytest.approx(r.income_tax / 5, abs=1e-9)
    assert r.resident_tax_monthly == pytest.approx(r.resident_tax / 4, abs=1e-9)
    assert r.consumption_tax_monthly == pytest.approx(r.consumption_tax / 5, abs=1e-9)


@pytest.mark.parametrize(
    "taxable, expected_rate, expected_deduction",
    [
        (195.0, 0.05, 0.0),      # 195万以下
        (195.01, 0.10, 9.75),    # 195万超
        (330.0, 0.10, 9.75),     # 330万以下
        (695.0, 0.20, 42.75),    # 695万以下
        (900.0, 0.23, 63.6),     # 900万以下
        (1800.0, 0.33, 153.6),   # 1800万以下
        (4000.0, 0.40, 279.6),   # 4000万以下
        (4000.01, 0.45, 479.6),  # 4000万超（上限なし）
    ],
)
def test_bracket_boundaries(taxable, expected_rate, expected_deduction):
    """累進区分は「〜超 〜以下」の境界を正しく判定する。"""
    b = select_bracket(taxable, STD_BRACKETS)
    assert b.rate == expected_rate
    assert b.deduction == expected_deduction


def test_manual_mode_requires_override_values():
    """manual モードで上書き値が欠けていればエラーになること。"""
    params = TaxParams(
        basic_deduction=58.0,
        blue_return_deduction=65.0,
        income_tax_rate_mode="manual",  # override 未設定
        brackets=STD_BRACKETS,
    )
    with pytest.raises(ValueError):
        calculate_tax(params, INPUTS_2025)
