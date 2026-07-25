"""生活費の既定マスタ（銀行・費目カテゴリ）の初期データと投入処理。

REQUIREMENTS.md §3 の分類例に基づく。いずれもユーザーが画面から追加・編集できる。
実際の金額は含まない（設定データのみ）ため seed 可。投入は冪等。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Bank, Category

# 銀行（口座）。現金は擬似口座（entry の bank_id を常に NOT NULL に保つため）。
DEFAULT_BANKS: list[str] = ["楽天銀行", "三井住友", "りそな", "UFJ", "現金"]

# 費目カテゴリ。(kind, 名前, 子[任意])。
# kind: income=収入 / expense=支払 / saving=積立・税金貯金
DEFAULT_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("income", "給与", []),
    ("income", "その他収入", []),
    ("expense", "食費", []),
    ("expense", "光熱費", ["電気", "ガス", "水道"]),
    ("expense", "保険", ["生命保険", "国民健康保険", "地震保険"]),
    ("expense", "国民年金", []),
    ("expense", "通信", []),
    ("expense", "NHK", []),
    ("expense", "くもん", []),
    ("expense", "返済", []),
    ("expense", "小遣い", []),
    ("expense", "その他", []),
    ("saving", "税金貯金", ["所得税", "住民税", "消費税", "固定資産税"]),
    ("saving", "積立貯金", ["iDeco", "小規模企業共済", "NISA", "年金保険", "現金貯蓄"]),
]


def seed_living_masters(session: Session) -> dict[str, int]:
    """銀行・カテゴリの既定値を投入する（冪等）。追加件数を返す。"""
    added_banks = 0
    for i, name in enumerate(DEFAULT_BANKS):
        exists = session.scalar(select(Bank.id).where(Bank.name == name))
        if exists is None:
            session.add(Bank(name=name, display_order=i))
            added_banks += 1

    added_categories = 0
    order = 0
    for kind, name, children in DEFAULT_CATEGORIES:
        parent = session.scalar(select(Category).where(Category.parent_id.is_(None), Category.name == name))
        if parent is None:
            parent = Category(name=name, kind=kind, parent_id=None, display_order=order)
            session.add(parent)
            session.flush()  # 子から参照するため id を確定
            added_categories += 1
        order += 1
        for cname in children:
            child_exists = session.scalar(
                select(Category.id).where(Category.parent_id == parent.id, Category.name == cname)
            )
            if child_exists is None:
                session.add(Category(name=cname, kind=kind, parent_id=parent.id, display_order=order))
                added_categories += 1
            order += 1

    session.commit()
    return {"banks": added_banks, "categories": added_categories}
