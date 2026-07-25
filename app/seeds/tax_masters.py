"""税年度マスタの初期データ（2025〜2027）と投入処理。

出典:
- tax_year_param … sample-file.xlsx「税金算出」シート row 9/10/11（2025/2026/2027）の手入力値。
  - 基礎控除(E) / 青色申告控除(K) / 復興税率(P) / 住民税率(R) / 所得税の手入力税率(N)・控除額(O)。
  - N/O が空欄の年度（2026/2027）は累進表からの自動判定（income_tax_rate_mode='auto'）とする。
- tax_bracket … 標準の累進税率表（REQUIREMENTS.md §4）。auto モードの区分判定に使用。

金額・控除額の単位はすべて万円。実際の申告金額（事業所得等）は tax_year_input 側であり、
実データのため本 seed には含めない。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaxBracket, TaxYearParam

# 標準の累進税率表（REQUIREMENTS.md §4）。(下限, 上限 or None, 税率, 控除額万円)
STANDARD_BRACKETS: list[tuple[float, float | None, float, float]] = [
    (0.0, 195.0, 0.05, 0.0),
    (195.0, 330.0, 0.10, 9.75),
    (330.0, 695.0, 0.20, 42.75),
    (695.0, 900.0, 0.23, 63.6),
    (900.0, 1800.0, 0.33, 153.6),
    (1800.0, 4000.0, 0.40, 279.6),
    (4000.0, None, 0.45, 479.6),
]

# 税年度パラメータ初期値（Excel row 9/10/11 由来）。
# 未指定キーはモデルの default（均等割0.5・消費税5%・分割5/4/5 等）を採用。
TAX_YEAR_PARAMS: list[dict] = [
    {
        # 2025: 所得税率は手入力（N9=0.20 / O9=60万）→ manual。復興0.021・住民0.10 は明示値。
        "year": 2025,
        "basic_deduction_manyen": 58.0,
        "blue_return_deduction_manyen": 65.0,
        "reconstruction_tax_rate": 0.021,
        "resident_tax_rate": 0.10,
        "resident_tax_deduction_manyen": 0.0,
        "income_tax_rate_mode": "manual",
        "income_tax_rate_override": 0.20,
        "income_tax_deduction_override": 60.0,
    },
    {
        # 2026: 所得税率の手入力なし → auto（累進表判定）。
        "year": 2026,
        "basic_deduction_manyen": 58.0,
        "blue_return_deduction_manyen": 65.0,
        "income_tax_rate_mode": "auto",
    },
    {
        # 2027: 青色申告控除は 75万（Excel K11=75）。所得税率は auto。
        "year": 2027,
        "basic_deduction_manyen": 58.0,
        "blue_return_deduction_manyen": 75.0,
        "income_tax_rate_mode": "auto",
    },
]


def seed_tax_masters(session: Session) -> dict[str, int]:
    """tax_year_param と tax_bracket を投入する（冪等）。

    既存年度はスキップし、追加した件数を返す。
    """
    added_params = 0
    added_brackets = 0

    for spec in TAX_YEAR_PARAMS:
        year = spec["year"]
        exists = session.get(TaxYearParam, year)
        if exists is None:
            session.add(TaxYearParam(**spec))
            added_params += 1

        # 当該年度の累進表が無ければ標準表を投入
        has_bracket = session.scalar(
            select(TaxBracket.id).where(TaxBracket.year == year).limit(1)
        )
        if has_bracket is None:
            for lower, upper, rate, ded in STANDARD_BRACKETS:
                session.add(
                    TaxBracket(
                        year=year,
                        lower_bound_manyen=lower,
                        upper_bound_manyen=upper,
                        rate=rate,
                        deduction_manyen=ded,
                    )
                )
                added_brackets += 1

    session.commit()
    return {"params": added_params, "brackets": added_brackets}
