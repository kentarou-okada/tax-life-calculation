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


def test_verification_table_2025(result_2025):
    """REQUIREMENTS.md §4 検証表と一致する（小数第2位で比較）。"""
    r = result_2025
    assert round(r.income, 2) == 975.0            # 所得金額
    assert round(r.taxable_income, 2) == 747.7    # 課税所得金額
    assert round(r.income_tax, 2) == 89.54        # 所得税額
    assert round(r.reconstruction_tax, 2) == 1.88  # 復興特別所得税
    assert round(r.resident_tax, 2) == 74.77      # 住民税
    assert round(r.consumption_tax, 2) == 48.75   # 消費税
    assert round(r.total_tax, 2) == 215.44        # 税金合計
    assert round(r.net_income, 2) == 747.76       # 手取り合計
    assert round(r.furusato_limit, 2) == 21.36    # ふるさと納税上限額


def test_income_tax_uses_manual_override_not_auto(result_2025):
    """§5-1: 課税所得747.7 は標準表なら23%区分だが、手動上書き20%/60万で89.54になること。

    自動判定（23%）を使うと別値になり、89.54 にはならないことを対比で確認する。
    """
    # 手動上書きの結果
    assert round(result_2025.income_tax_rate, 2) == 0.20
    assert round(result_2025.income_tax, 2) == 89.54

    # 同一入力を auto（標準表）で計算すると 23% 区分になり 89.54 と一致しない
    params_auto = TaxParams(
        basic_deduction=58.0,
        blue_return_deduction=65.0,
        income_tax_rate_mode="auto",
        brackets=STD_BRACKETS,
    )
    r_auto = calculate_tax(params_auto, INPUTS_2025)
    assert r_auto.income_tax_rate == 0.23                 # 695〜900万 区分
    assert round(r_auto.income_tax, 2) == 108.37          # 747.7*0.23-63.6
    assert round(r_auto.income_tax, 2) != round(result_2025.income_tax, 2)


def test_auto_judgment_selects_23pct_for_747(result_2025):
    """課税所得747.7 の自動判定が 695〜900万（23%/控除63.6）区分を選ぶこと。"""
    rate, deduction = resolve_income_tax_rate(
        TaxParams(basic_deduction=58.0, blue_return_deduction=65.0, brackets=STD_BRACKETS),
        747.7,
    )
    assert rate == 0.23
    assert deduction == 63.6


def test_monthly_splits_2025(result_2025):
    """税額の毎月分割（Excel R6/S6/W6）。"""
    r = result_2025
    assert r.income_tax_monthly == pytest.approx(89.54 / 5, abs=1e-6)       # 17.908
    assert r.resident_tax_monthly == pytest.approx(74.77 / 4, abs=1e-6)     # 18.6925
    assert r.consumption_tax_monthly == pytest.approx(48.75 / 5, abs=1e-6)  # 9.75


def test_remaining_2025(result_2025):
    """各種支払後残金（Excel F6 = 566.45966）。"""
    assert round(result_2025.remaining, 2) == 566.46


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
