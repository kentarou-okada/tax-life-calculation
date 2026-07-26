"""生活費収支の集計ロジック（純粋関数）。UI・DB から独立。単位＝円（整数）。

REQUIREMENTS.md §3:
- 総収入 / 総支払 / 余剰金（＝総収入 − 総支払）を月次で算出
- 銀行ごとの内訳合計
- 年次サマリ（月次の積み上げ）
費目の収入/支払/貯蓄の別は category.kind（income/expense/saving）で判定する。
支払（総支払）は expense と saving（税金貯金・積立）の流出合計とする（Excel の 余剰金 定義に一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class EntryRow:
    """集計対象の 1 エントリ。kind は費目カテゴリの種別。

    saving_group は kind=saving のとき 'tax'（税金貯金）/ 'reserve'（積立貯金）を表す。
    最上位の親カテゴリ（税金貯金 / 積立貯金）から決まる。
    """

    bank_id: int
    category_id: int
    kind: str  # 'income' | 'expense' | 'saving'
    month: int
    amount_yen: int
    saving_group: str = ""  # 'tax' | 'reserve' | ''


@dataclass
class MonthAggregate:
    """1 か月分の集計（円）。"""

    income: int = 0            # 総収入（kind=income）
    expense: int = 0          # 支払（kind=expense）
    saving: int = 0           # 貯蓄合計（税金貯金＋積立貯金）
    saving_tax: int = 0       # うち 税金貯金
    saving_reserve: int = 0   # うち 積立貯金
    by_bank_outflow: dict[int, int] = field(default_factory=dict)  # 銀行別の流出合計（expense+saving）
    by_bank_income: dict[int, int] = field(default_factory=dict)   # 銀行別の収入合計

    @property
    def payment(self) -> int:
        """総支払（流出合計）＝ expense + saving。"""
        return self.expense + self.saving

    @property
    def surplus(self) -> int:
        """余剰金＝総収入 − 総支払。"""
        return self.income - self.payment


def _accumulate(agg: MonthAggregate, r: EntryRow) -> None:
    if r.kind == "income":
        agg.income += r.amount_yen
        agg.by_bank_income[r.bank_id] = agg.by_bank_income.get(r.bank_id, 0) + r.amount_yen
        return
    # expense / saving は流出
    if r.kind == "saving":
        agg.saving += r.amount_yen
        if r.saving_group == "tax":
            agg.saving_tax += r.amount_yen
        elif r.saving_group == "reserve":
            agg.saving_reserve += r.amount_yen
    else:
        agg.expense += r.amount_yen
    agg.by_bank_outflow[r.bank_id] = agg.by_bank_outflow.get(r.bank_id, 0) + r.amount_yen


def aggregate_month(rows: Iterable[EntryRow]) -> MonthAggregate:
    """単一月のエントリ群を集計する。"""
    agg = MonthAggregate()
    for r in rows:
        _accumulate(agg, r)
    return agg


@dataclass
class YearAggregate:
    """年次サマリ。months[1..12] の各月集計と年間合計。"""

    months: dict[int, MonthAggregate] = field(default_factory=dict)
    total: MonthAggregate = field(default_factory=MonthAggregate)


def aggregate_by_category(rows: Iterable[EntryRow]) -> dict[int, int]:
    """費目（category_id）ごとの合計（円）。年間の費目別集計に使う。"""
    totals: dict[int, int] = {}
    for r in rows:
        totals[r.category_id] = totals.get(r.category_id, 0) + r.amount_yen
    return totals


def aggregate_year(rows: Iterable[EntryRow]) -> YearAggregate:
    """1 年分（複数月）のエントリを月別・年計で集計する。"""
    ya = YearAggregate()
    for m in range(1, 13):
        ya.months[m] = MonthAggregate()
    for r in rows:
        month_agg = ya.months.setdefault(r.month, MonthAggregate())
        _accumulate(month_agg, r)
        _accumulate(ya.total, r)
    return ya


def round10(value: float) -> int:
    """10 円単位に丸める（生活費の入力・表示規約）。四捨五入（round half up）で決定的にする。

    Python 標準 round は偶数丸め（banker's rounding）で .5 の扱いが直感と食い違い、
    float 演算の微差で結果が揺れるため、非負の金額を素直に四捨五入する。
    """
    return int(math.floor(value / 10.0 + 0.5)) * 10


def manyen_to_yen10(manyen: float) -> int:
    """万円（float）→ 円（10円単位、整数）。"""
    return round10(manyen * 10_000)


@dataclass(frozen=True)
class TaxSavingPlan:
    """税金貯金の毎月積立目安（円）。§4 の分割式と、年額を12分割した目安の両方を提示する。"""

    income_tax_per_split: int      # 所得税 ÷ 分割数（1回あたり）
    resident_tax_per_split: int    # 住民税 ÷ 分割数
    consumption_tax_per_split: int  # 消費税 ÷ 分割数
    income_tax_split_count: int
    resident_tax_split_count: int
    consumption_tax_split_count: int
    annual_total: int              # 年間税額合計
    monthly_reserve: int           # 毎月の積立目安（年間税額 ÷ 12）


def tax_saving_plan(
    income_tax_manyen: float,
    resident_tax_manyen: float,
    consumption_tax_manyen: float,
    total_tax_manyen: float,
    income_tax_split_count: int,
    resident_tax_split_count: int,
    consumption_tax_split_count: int,
) -> TaxSavingPlan:
    """税額（万円）と分割数から、税金貯金の目安（円）を組む。"""
    return TaxSavingPlan(
        income_tax_per_split=manyen_to_yen10(income_tax_manyen / income_tax_split_count),
        resident_tax_per_split=manyen_to_yen10(resident_tax_manyen / resident_tax_split_count),
        consumption_tax_per_split=manyen_to_yen10(consumption_tax_manyen / consumption_tax_split_count),
        income_tax_split_count=income_tax_split_count,
        resident_tax_split_count=resident_tax_split_count,
        consumption_tax_split_count=consumption_tax_split_count,
        annual_total=manyen_to_yen10(total_tax_manyen),
        monthly_reserve=manyen_to_yen10(total_tax_manyen / 12),
    )
