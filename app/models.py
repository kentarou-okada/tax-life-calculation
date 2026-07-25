"""SQLAlchemy モデル。docs/SCHEMA.md の DDL と 1:1 に対応する。

単位の規約（CLAUDE.md 準拠）:
- 生活費（機能A）の金額 = 円・整数（amount_yen）
- 税金（機能B）の金額 = 万円・float（列名サフィックス _manyen）
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

_NOW = text("CURRENT_TIMESTAMP")


# --------------------------------------------------------------------------- #
# 機能A：生活費収支
# --------------------------------------------------------------------------- #
class Bank(Base):
    """銀行／口座マスタ。現金支払いは name='現金' の擬似口座で表現する。"""

    __tablename__ = "bank"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)

    entries: Mapped[list["MonthlyEntry"]] = relationship(back_populates="bank")


class Category(Base):
    """費目カテゴリ。kind で収入/支払/貯蓄を区別し、parent_id で階層を表す。"""

    __tablename__ = "category"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_category_parent_name"),
        CheckConstraint("kind IN ('income','expense','saving')", name="ck_category_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="expense", server_default=text("'expense'"))
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("category.id", ondelete="RESTRICT"), nullable=True
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)

    parent: Mapped[Optional["Category"]] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    entries: Mapped[list["MonthlyEntry"]] = relationship(back_populates="category")


class MonthlyEntry(Base):
    """月次エントリ：銀行 × カテゴリ × 年月 × 金額（円・10円単位・非負）。"""

    __tablename__ = "monthly_entry"
    __table_args__ = (
        UniqueConstraint("bank_id", "category_id", "year", "month", name="uq_entry_bank_cat_period"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_entry_month"),
        CheckConstraint("amount_yen >= 0 AND amount_yen % 10 = 0", name="ck_entry_amount"),
        Index("idx_entry_period", "year", "month"),
        Index("idx_entry_category", "category_id"),
        Index("idx_entry_bank", "bank_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_id: Mapped[int] = mapped_column(ForeignKey("bank.id", ondelete="RESTRICT"), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="RESTRICT"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_yen: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)

    bank: Mapped["Bank"] = relationship(back_populates="entries")
    category: Mapped["Category"] = relationship(back_populates="entries")


# --------------------------------------------------------------------------- #
# 機能B：税金算出（Excel を正とする。単位は万円）
# --------------------------------------------------------------------------- #
class TaxYearParam(Base):
    """税年度パラメータ（年度別マスタ）。基礎控除・青色・均等割・各種率・方式・分割数。"""

    __tablename__ = "tax_year_param"
    __table_args__ = (
        CheckConstraint("income_tax_rate_mode IN ('auto','manual')", name="ck_param_rate_mode"),
        CheckConstraint(
            "consumption_tax_method IN ('income_5pct','invoice_2wari','none')",
            name="ck_param_consumption_method",
        ),
        CheckConstraint("furusato_method IN ('simple','precise')", name="ck_param_furusato_method"),
    )

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # 所得控除の年度標準値（万円）
    basic_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=48, server_default=text("48"))
    blue_return_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=65, server_default=text("65"))
    # 固定・率パラメータ
    flat_rate_tax_manyen: Mapped[float] = mapped_column(nullable=False, default=0.5, server_default=text("0.5"))
    reconstruction_tax_rate: Mapped[float] = mapped_column(nullable=False, default=0.021, server_default=text("0.021"))
    resident_tax_rate: Mapped[float] = mapped_column(nullable=False, default=0.10, server_default=text("0.1"))
    resident_tax_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 所得税率：累進表からの自動判定 or 年度手動上書き
    income_tax_rate_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="auto", server_default=text("'auto'")
    )
    income_tax_rate_override: Mapped[Optional[float]] = mapped_column(nullable=True)
    income_tax_deduction_override: Mapped[Optional[float]] = mapped_column(nullable=True)
    # 消費税・ふるさと納税の算定方式
    consumption_tax_method: Mapped[str] = mapped_column(
        String, nullable=False, default="income_5pct", server_default=text("'income_5pct'")
    )
    consumption_tax_rate: Mapped[float] = mapped_column(nullable=False, default=0.05, server_default=text("0.05"))
    furusato_method: Mapped[str] = mapped_column(
        String, nullable=False, default="simple", server_default=text("'simple'")
    )
    # 税額の毎月分割数
    income_tax_split_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=text("5"))
    resident_tax_split_count: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default=text("4"))
    consumption_tax_split_count: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=text("5"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)

    brackets: Mapped[list["TaxBracket"]] = relationship(
        back_populates="param", cascade="all, delete-orphan", order_by="TaxBracket.lower_bound_manyen"
    )
    tax_input: Mapped[Optional["TaxYearInput"]] = relationship(
        back_populates="param", cascade="all, delete-orphan", uselist=False
    )


class TaxBracket(Base):
    """累進税率テーブル（年度別）。auto モード時に課税所得から区分判定する。"""

    __tablename__ = "tax_bracket"
    __table_args__ = (
        UniqueConstraint("year", "lower_bound_manyen", name="uq_bracket_year_lower"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(
        ForeignKey("tax_year_param.year", ondelete="CASCADE"), nullable=False
    )
    lower_bound_manyen: Mapped[float] = mapped_column(nullable=False)
    upper_bound_manyen: Mapped[Optional[float]] = mapped_column(nullable=True)  # NULL=上限なし
    rate: Mapped[float] = mapped_column(nullable=False)
    deduction_manyen: Mapped[float] = mapped_column(nullable=False)

    param: Mapped["TaxYearParam"] = relationship(back_populates="brackets")


class TaxYearInput(Base):
    """税年度入力（その年の申告値）。実際の金額を含むため seed はしない（ローカルで入力）。"""

    __tablename__ = "tax_year_input"

    year: Mapped[int] = mapped_column(
        ForeignKey("tax_year_param.year", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    # 収入・経費（万円）
    business_income_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    salary_income_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    salary_revenue_manyen: Mapped[Optional[float]] = mapped_column(nullable=True)  # 任意（§5-5 将来）
    expenses_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 各種所得控除（万円）※基礎控除・青色は TaxYearParam 側
    spouse_special_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    life_insurance_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    social_insurance_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    small_biz_mutual_aid_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    other_income_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    earthquake_insurance_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 住宅ローン控除（税額控除・所得税額から差引）
    housing_loan_deduction_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 寄付金
    donation_manyen: Mapped[float] = mapped_column(nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)

    param: Mapped["TaxYearParam"] = relationship(back_populates="tax_input")
