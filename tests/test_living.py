"""生活費集計ロジックの検証（REQUIREMENTS.md §3）。"""
from __future__ import annotations

from app.core.living import (
    EntryRow,
    aggregate_month,
    aggregate_year,
    manyen_to_yen10,
    round10,
    tax_saving_plan,
)


def _rows_one_month(month: int = 3) -> list[EntryRow]:
    # 三井住友(2): 給与 850000(income), 食費 68000(expense)
    # 楽天(1): 所得税(税金貯金) 130000, iDeco(積立貯金) 20000, 電気 12340(expense)
    return [
        EntryRow(bank_id=2, category_id=1, kind="income", month=month, amount_yen=850000),
        EntryRow(bank_id=2, category_id=3, kind="expense", month=month, amount_yen=68000),
        EntryRow(bank_id=1, category_id=19, kind="saving", month=month, amount_yen=130000, saving_group="tax"),
        EntryRow(bank_id=1, category_id=25, kind="saving", month=month, amount_yen=20000, saving_group="reserve"),
        EntryRow(bank_id=1, category_id=5, kind="expense", month=month, amount_yen=12340),
    ]


def test_aggregate_month_totals():
    agg = aggregate_month(_rows_one_month())
    assert agg.income == 850000
    assert agg.expense == 80340          # 68000 + 12340
    assert agg.saving == 150000          # 130000 + 20000
    assert agg.saving_tax == 130000      # 税金貯金
    assert agg.saving_reserve == 20000   # 積立貯金
    assert agg.payment == 230340         # expense + saving
    assert agg.surplus == 619660         # 収入 - 総支払


def test_aggregate_month_bank_breakdown():
    agg = aggregate_month(_rows_one_month())
    # 流出（expense+saving）: 三井住友=68000, 楽天=130000+20000+12340
    assert agg.by_bank_outflow[2] == 68000
    assert agg.by_bank_outflow[1] == 162340
    # 収入: 三井住友=850000
    assert agg.by_bank_income[2] == 850000


def test_aggregate_year_rolls_up_months():
    rows = _rows_one_month(3) + _rows_one_month(4)
    ya = aggregate_year(rows)
    assert set(ya.months) >= set(range(1, 13))       # 12か月ぶん存在
    assert ya.months[3].surplus == 619660
    assert ya.months[5].income == 0                  # 未入力月は 0
    assert ya.total.income == 1700000                # 850000 * 2
    assert ya.total.payment == 460680                # 230340 * 2
    assert ya.total.saving_tax == 260000             # 130000 * 2
    assert ya.total.saving_reserve == 40000          # 20000 * 2
    assert ya.total.by_bank_outflow[1] == 324680     # 162340 * 2


def test_round10_and_manyen():
    assert round10(12345) == 12350                   # 四捨五入（half up）
    assert round10(12344) == 12340
    assert round10(12346) == 12350
    assert manyen_to_yen10(9.75) == 97500            # 消費税分割 例
    assert manyen_to_yen10(17.908) == 179080         # 所得税89.54 ÷ 5


def test_tax_saving_plan_matches_section4_splits():
    # 2025 の税額（万円）: 所得税89.54 / 住民74.77 / 消費48.75 / 合計215.44、分割 5/4/5
    plan = tax_saving_plan(
        income_tax_manyen=89.54,
        resident_tax_manyen=74.77,
        consumption_tax_manyen=48.75,
        total_tax_manyen=215.44,
        income_tax_split_count=5,
        resident_tax_split_count=4,
        consumption_tax_split_count=5,
    )
    assert plan.income_tax_per_split == 179080        # 89.54/5 万円 → 円
    assert plan.resident_tax_per_split == 186930      # 74.77/4 = 18.6925万 → 186930（10円丸め）
    assert plan.consumption_tax_per_split == 97500    # 48.75/5 = 9.75万
    assert plan.annual_total == 2154400               # 215.44万
    assert plan.monthly_reserve == 179530             # 2,154,400 / 12 → 10円丸め
