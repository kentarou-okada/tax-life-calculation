"""税金算出ロジック（純粋関数）。UI・DB から独立。

正（source of truth）: REQUIREMENTS.md §4 と sample-file.xlsx「税金算出」シートの数式。
単位はすべて **万円**（float）。丸めは行わず、表示側で丸める。

Excel 数式との対応（2025年・row 9）:
    所得金額 C7 = B9+C9-D9-K9
    課税所得 F7 = C7-SUM(E9:L9)+K9      （青色 K は所得金額で控除済みのため課税所得では相殺）
    所得税額 O7 = F7*N9-O9              （税率・控除額は auto=累進表 / manual=手動上書き）
    復興    Q7 = O7*P9
    住民税   S7 = F7*R9-S9
    市県民税 U7 = 0.5（均等割・固定）
    消費税   W7 = C7*0.05（簡易見積り。方式切替可）
    税金合計 O6 = O7+Q7+S7+U7+W7
    手取り   C6 = C7-O6-H9+K9
    支払後残 F6 = C6-H9-I9-J9-G9-L9-M9
    ふるさと M6 = F7/10*0.2*10/7 = F7*0.2/7
    分割     R6=O7/5, S6=S7/4, W6=W7/5
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bracket:
    """累進税率区分。lower 超 upper 以下（upper=None は上限なし）。単位=万円。"""

    lower: float
    upper: float | None
    rate: float
    deduction: float


@dataclass(frozen=True)
class TaxParams:
    """年度別パラメータ（tax_year_param 相当）。単位=万円。"""

    basic_deduction: float                       # 基礎控除
    blue_return_deduction: float                 # 青色申告控除額
    flat_rate_tax: float = 0.5                   # 市県民税(均等割)
    reconstruction_tax_rate: float = 0.021       # 復興特別所得税率
    resident_tax_rate: float = 0.10              # 住民税率
    resident_tax_deduction: float = 0.0          # 住民税控除額
    # 所得税率：auto=累進表から自動判定 / manual=手動上書き値を使用（§5-1）
    income_tax_rate_mode: str = "auto"
    income_tax_rate_override: float | None = None
    income_tax_deduction_override: float | None = None
    brackets: tuple[Bracket, ...] = field(default_factory=tuple)
    # 算定方式
    consumption_tax_method: str = "income_5pct"  # income_5pct | invoice_2wari | none
    consumption_tax_rate: float = 0.05
    furusato_method: str = "simple"              # simple | precise
    # 税額の毎月分割数
    income_tax_split_count: int = 5
    resident_tax_split_count: int = 4
    consumption_tax_split_count: int = 5


@dataclass(frozen=True)
class TaxInputs:
    """その年の申告入力（tax_year_input 相当）。単位=万円。"""

    business_income: float = 0.0                 # 事業所得
    salary_income: float = 0.0                   # 給与所得
    expenses: float = 0.0                        # 経費
    spouse_special_deduction: float = 0.0        # 配偶者特別控除
    life_insurance_deduction: float = 0.0        # 生命保険料控除
    social_insurance_deduction: float = 0.0      # 社会保険控除
    small_biz_mutual_aid_deduction: float = 0.0  # 小規模企業共済等掛金控除
    other_income_deduction: float = 0.0          # その他所得控除（Excel 列J）
    earthquake_insurance_deduction: float = 0.0  # 地震保険料控除
    housing_loan_deduction: float = 0.0          # 住宅ローン控除（税額控除・所得税額から直接差引）
    donation: float = 0.0                        # 寄付金（ふるさと納税額）


# ふるさと納税の自己負担額（2000円 = 0.2万円）。この額を超えた分が税額控除の対象。
FURUSATO_SELF_PAY_MANYEN = 0.2


@dataclass(frozen=True)
class TaxResult:
    """税額算出結果（万円）。丸めは行わない。"""

    income: float                    # 所得金額
    taxable_income: float            # 課税所得金額
    income_tax_rate: float           # 適用税率（判定結果）
    income_tax_deduction: float      # 適用控除額
    income_tax_before_credits: float  # 所得税額（税額控除前 = 課税所得×税率−控除額）
    housing_loan_credit: float       # 住宅ローン控除（所得税から差引いた額）
    furusato_income_tax_credit: float  # ふるさと納税の所得税分控除
    furusato_resident_credit: float  # ふるさと納税の住民税分控除
    income_tax: float                # 所得税額（住宅ローン・ふるさと控除後）
    reconstruction_tax: float        # 復興特別所得税
    resident_tax: float              # 住民税（ふるさと控除後）
    flat_rate_tax: float             # 市県民税(均等割)
    consumption_tax: float           # 消費税
    total_tax: float                 # 税金合計
    net_income: float                # 税金及び経費支払い後残金（事業所得−経費−税金合計）
    remaining: float                 # 社会保険料支払後合計（上記−社会保険控除）
    furusato_limit: float            # ふるさと納税上限額
    income_tax_monthly: float        # 所得税分割
    resident_tax_monthly: float      # 住民税分割
    consumption_tax_monthly: float   # 消費税分割


def select_bracket(taxable_income: float, brackets: tuple[Bracket, ...]) -> Bracket:
    """課税所得から累進区分を判定する。「〜以下」を含む側で最初に該当する区分を返す。"""
    if not brackets:
        raise ValueError("累進税率表（brackets）が空です。auto 判定には区分が必要です。")
    ordered = sorted(brackets, key=lambda b: b.lower)
    for b in ordered:
        if b.upper is None or taxable_income <= b.upper:
            return b
    return ordered[-1]  # 上限超過は最終区分（通常 upper=None のはず）


def resolve_income_tax_rate(params: TaxParams, taxable_income: float) -> tuple[float, float]:
    """適用する所得税の (税率, 控除額) を返す。

    manual モードかつ上書き値が揃っていれば手動値を、そうでなければ累進表から自動判定する。
    """
    if params.income_tax_rate_mode == "manual":
        if params.income_tax_rate_override is None or params.income_tax_deduction_override is None:
            raise ValueError("manual モードには income_tax_rate_override と income_tax_deduction_override が必要です。")
        return params.income_tax_rate_override, params.income_tax_deduction_override
    b = select_bracket(taxable_income, params.brackets)
    return b.rate, b.deduction


def _consumption_tax(params: TaxParams, income: float) -> float:
    method = params.consumption_tax_method
    if method == "income_5pct":
        return income * params.consumption_tax_rate  # Excel: C7*0.05
    if method == "none":
        return 0.0
    if method == "invoice_2wari":
        raise NotImplementedError("invoice_2wari（インボイス2割特例）は売上税額の入力が必要。未実装。")
    raise ValueError(f"未知の consumption_tax_method: {method}")


def _furusato_limit(
    params: TaxParams, taxable_income: float, resident_income_levy: float, income_tax_rate: float
) -> float:
    method = params.furusato_method
    if method == "simple":
        return taxable_income * 0.2 / 7  # Excel: F7*0.2/7
    if method == "precise":
        # 住民税所得割 × 20% ÷（90% − 所得税率×1.021）+ 0.2万(2000円)
        denominator = 0.9 - income_tax_rate * 1.021
        if denominator <= 0:
            raise ValueError("precise 式の分母が非正です（所得税率が高すぎます）。")
        return resident_income_levy * 0.2 / denominator + 0.2
    raise ValueError(f"未知の furusato_method: {method}")


def calculate_tax(params: TaxParams, inputs: TaxInputs) -> TaxResult:
    """税額を算出する。副作用なし。"""
    # 所得金額（青色申告控除は現金流出でないが所得計算では差し引く）
    income = (
        inputs.business_income
        + inputs.salary_income
        - inputs.expenses
        - params.blue_return_deduction
    )

    # 課税所得（青色は差し引かない＝所得金額で控除済み。寄付金も差し引かない）
    deductions = (
        params.basic_deduction
        + inputs.spouse_special_deduction
        + inputs.life_insurance_deduction
        + inputs.social_insurance_deduction
        + inputs.small_biz_mutual_aid_deduction
        + inputs.other_income_deduction
        + inputs.earthquake_insurance_deduction
    )
    taxable_income = income - deductions

    # 所得税額（控除前。税率・控除額は auto/manual で解決）。負値はガードして 0 に丸める。
    rate, rate_deduction = resolve_income_tax_rate(params, taxable_income)
    income_tax_before_credits = max(0.0, taxable_income * rate - rate_deduction)

    # ふるさと納税の税額控除：自己負担2000円を超えた額を、所得税分（×税率）と
    # 住民税分（×(1−税率)＝基本分10%＋特例分(90%−税率)）に配分し、それぞれから差し引く。
    furusato_base = max(0.0, inputs.donation - FURUSATO_SELF_PAY_MANYEN)
    furusato_income_tax_credit = furusato_base * rate
    furusato_resident_credit = furusato_base * (1.0 - rate)

    # 住宅ローン控除（税額控除）。所得税額から直接差し引く（0 の可能性あり）。
    housing_loan_credit = max(0.0, inputs.housing_loan_deduction)

    # 所得税額（住宅ローン控除・ふるさと納税所得税分を差し引き、0 でガード）
    income_tax = max(0.0, income_tax_before_credits - housing_loan_credit - furusato_income_tax_credit)

    # 復興特別所得税は控除後の所得税額を基準とする（Excel の基準所得税額の考え方）
    reconstruction_tax = income_tax * params.reconstruction_tax_rate

    # 住民税（所得割）→ ふるさと納税住民税分を差し引き、0 でガード
    resident_tax_before = max(0.0, taxable_income * params.resident_tax_rate - params.resident_tax_deduction)
    resident_tax = max(0.0, resident_tax_before - furusato_resident_credit)

    flat_rate_tax = params.flat_rate_tax
    consumption_tax = _consumption_tax(params, income)

    total_tax = income_tax + reconstruction_tax + resident_tax + flat_rate_tax + consumption_tax

    # 税金及び経費支払い後残金 ＝ 事業所得 − 経費 − 税金合計
    net_income = inputs.business_income - inputs.expenses - total_tax
    # 社会保険料支払後合計 ＝ 上記 − 社会保険控除
    remaining = net_income - inputs.social_insurance_deduction

    # ふるさと納税上限額は住民税所得割（控除前）を基準に算定
    furusato_limit = _furusato_limit(params, taxable_income, resident_tax_before, rate)

    return TaxResult(
        income=income,
        taxable_income=taxable_income,
        income_tax_rate=rate,
        income_tax_deduction=rate_deduction,
        income_tax_before_credits=income_tax_before_credits,
        housing_loan_credit=housing_loan_credit,
        furusato_income_tax_credit=furusato_income_tax_credit,
        furusato_resident_credit=furusato_resident_credit,
        income_tax=income_tax,
        reconstruction_tax=reconstruction_tax,
        resident_tax=resident_tax,
        flat_rate_tax=flat_rate_tax,
        consumption_tax=consumption_tax,
        total_tax=total_tax,
        net_income=net_income,
        remaining=remaining,
        furusato_limit=furusato_limit,
        income_tax_monthly=income_tax / params.income_tax_split_count,
        resident_tax_monthly=resident_tax / params.resident_tax_split_count,
        consumption_tax_monthly=consumption_tax / params.consumption_tax_split_count,
    )
