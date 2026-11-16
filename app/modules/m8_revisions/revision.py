"""M8 修订重算（纯计算）：在内存里把更正变更并入原变更序列，重建派生结构。

不碰数据库：入参出参都是普通 dict/list，与 M4/M5/M6 共用同一套分段、
最大余额法、出账规则，保证修订后的账单和「采信时就算到这些变更」逐分一致。
原账单是否被覆盖由服务层的版本快照负责，这里只回答「正确结果应该是多少」。
"""
from __future__ import annotations

from decimal import Decimal

from ..m4_proration import prorate, rounding
from ..m5_changes import credit, segments as segments_mod
from ..m6_invoices import invoice as invoice_mod

REVISION_FLAG = "R_REVISION_CORRECTION"


def _index(rows: list[dict], key: str) -> dict:
    return {row[key]: row for row in rows}


def merge_changes(base_changes: list[dict], correction_rows: list[dict]) -> list[dict]:
    """原变更 + 更正变更合并；同订阅同本地日由更正变更替代原生效条。

    替代而不是并存：同日只允许一条生效（沿用 M3 的硬规则），更正代表事后认定的
    正确事实，因此把当日所有原条目标记为被替代，证据仍保留在原行里可追溯。
    一个本地日可能对应多条原变更（同日对决），所以按日整体替代，不能逐条覆盖。
    """
    correction_days: dict[tuple[str, str], list[dict]] = {}
    for row in correction_rows:
        correction_days.setdefault(
            (row["sub_id"], row["local_date"]), []).append(row)
    winner_by_day = {
        key: min(rows, key=lambda item: (item["effective_utc"], item["change_id"],
                                         item["seq"]))
        for key, rows in correction_days.items()
    }

    merged: list[dict] = []
    for change in base_changes:
        day_key = (change["sub_id"], change["local_date"])
        if day_key in winner_by_day:
            winner = winner_by_day[day_key]
            replaced = dict(change)
            replaced["applied"] = 0
            replaced["superseded_by"] = winner["change_id"]
            if REVISION_FLAG not in replaced["flags"]:
                replaced["flags"] = [*replaced["flags"], REVISION_FLAG]
            merged.append(replaced)
        else:
            merged.append(dict(change))

    merged.extend(dict(row) for row in correction_rows)
    merged.sort(key=lambda item: (item["sub_id"], item["local_date"],
                                  item["effective_utc"], item["change_id"], item["seq"]))
    return merged


def compute(subs: list[dict], base_changes: list[dict],
            correction_rows: list[dict], settings) -> dict:
    """并入更正后重建分段、抵扣行、账单；金额口径与 M4/M5/M6 完全相同。"""
    changes = merge_changes(base_changes, correction_rows)
    by_sub: dict[str, list[dict]] = {}
    for change in changes:
        by_sub.setdefault(change["sub_id"], []).append(change)

    segment_rows: list[dict] = []
    component_rows: list[dict] = []
    invoices: list[dict] = []
    for sub in subs:
        invoice_id = invoice_mod.invoice_id_for(sub)
        built = segments_mod.build_segments(sub, by_sub.get(sub["sub_id"], []))
        seg_rows = segments_mod.segment_rows(invoice_id, sub, built)
        period_days = int(sub["period_days"])

        exacts = [
            prorate.segment_amount(Decimal(row["monthly"]), int(row["days"]),
                                   period_days, row["state"])
            for row in seg_rows
        ]
        cents = rounding.allocate(exacts)
        for row, exact, value in zip(seg_rows, exacts, cents):
            row["amount_exact"] = prorate.exact_text(exact)
            row["amount_cents"] = value
        segment_rows.extend(seg_rows)

        comp_rows = []
        for component in credit.build_components(built, period_days):
            comp_rows.append({
                "invoice_id": invoice_id,
                "sub_id": sub["sub_id"],
                "seq": component.seq,
                "kind": component.kind,
                "source": component.source,
                "label": component.label,
                "days": component.days,
                "monthly": str(component.monthly),
                # 与 components 表同构：build_invoice 读 exact_text
                "exact_text": prorate.exact_text(component.exact),
                "exact": prorate.exact_text(component.exact),
                "cents": 0,
                "flags": component.flags,
            })
        comp_cents = rounding.allocate([Decimal(row["exact_text"]) for row in comp_rows])
        for row, value in zip(comp_rows, comp_cents):
            row["cents"] = value
        component_rows.extend(comp_rows)

        invoice = invoice_mod.build_invoice(sub, seg_rows, comp_rows)
        affected = any(REVISION_FLAG in segment.get("flags", []) for segment in seg_rows)
        if affected and REVISION_FLAG not in invoice["flags"]:
            invoice["flags"] = [*invoice["flags"], REVISION_FLAG]
        invoices.append(invoice)

    return {
        "changes": changes,
        "segments": segment_rows,
        "components": component_rows,
        "invoices": invoices,
    }
