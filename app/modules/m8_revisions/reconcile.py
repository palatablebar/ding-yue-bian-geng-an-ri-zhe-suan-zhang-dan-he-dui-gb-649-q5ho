"""M8 修订对账：在原四本账之外加一本「修订账」，并给出逐账单调整单。

修订账守恒式（单位：分）：
    Σ调整差额 == Σ当前账单额 − Σ原账单额（首个已发布版本）
逐张调整单恒等式：
    before_cents + delta_cents == after_cents
差额符号约定：delta > 0 应补收（surcharge），delta < 0 应退（refund），0 不变。
"""
from __future__ import annotations

from ... import manifest


def invoice_hash(invoice: dict) -> str:
    """账单全字段哈希：版本快照用它证明旧版逐字未变。"""
    parts = [
        invoice["invoice_id"], invoice["sub_id"], invoice.get("account", ""),
        invoice["period_start"], invoice["period_end"],
        str(invoice["period_days"]), str(invoice["billed_days"]),
        str(invoice["free_days"]), str(invoice["charge_cents"]),
        str(invoice["credit_cents"]), str(invoice["total_cents"]),
        str(invoice.get("exact_total", "0")), invoice.get("status", ""),
        "|".join(invoice.get("flags", [])),
    ]
    return manifest.digest_parts(parts)[:24]


def direction_of(delta_cents: int) -> str:
    if delta_cents > 0:
        return "surcharge"
    if delta_cents < 0:
        return "refund"
    return "unchanged"


def build_adjustments(*, before_invoices: list[dict], after_invoices: list[dict],
                      corrections_by_invoice: dict[str, list[str]]) -> list[dict]:
    """逐张账单对比出调整单；更正涉及的订阅即使差额为 0 也留痕。

    ``corrections_by_invoice`` 给出每张受影响账单实际依据的更正号，
    未受影响账单记整批更正号，保证调整单与更正依据能逐张对上。
    """
    before = {row["invoice_id"]: row for row in before_invoices}
    after = {row["invoice_id"]: row for row in after_invoices}
    all_ids = sorted({cid for ids in corrections_by_invoice.values() for cid in ids})
    fallback = ",".join(all_ids)
    adjustments: list[dict] = []
    for invoice_id in sorted(set(before) | set(after)):
        old = before.get(invoice_id)
        new = after.get(invoice_id)
        before_cents = int(old["total_cents"]) if old else 0
        after_cents = int(new["total_cents"]) if new else 0
        delta = after_cents - before_cents
        sub_id = (new or old)["sub_id"]
        account = (new or old).get("account", "")
        correction_text = ",".join(corrections_by_invoice.get(invoice_id, [])) or fallback
        note = (f"{correction_text} 重算：原 {before_cents} 分 → 新 {after_cents} 分"
                if correction_text else f"原 {before_cents} 分 → 新 {after_cents} 分")
        adjustments.append({
            "correction_id": correction_text,
            "invoice_id": invoice_id,
            "sub_id": sub_id,
            "account": account,
            "before_cents": before_cents,
            "after_cents": after_cents,
            "delta_cents": delta,
            "direction": direction_of(delta),
            "note": note,
        })
    return adjustments


def revision_ledger(adjustments: list[dict], before_invoices: list[dict],
                    after_invoices: list[dict]) -> dict:
    """修订账：左 = Σ调整差额，右 = 当前账单合计 − 原账单合计。"""
    left = sum(int(row["delta_cents"]) for row in adjustments)
    right = (sum(int(row["total_cents"]) for row in after_invoices)
             - sum(int(row["total_cents"]) for row in before_invoices))
    diff = left - right
    return {
        "ledger": "revision", "label": "修订账",
        "left_text": str(left), "right_text": str(right),
        "diff_text": str(diff), "status": "ok" if diff == 0 else "mismatch",
        "note": "Σ调整差额 = 当前账单 − 原账单（正补负退，单位分）",
    }


def verify_adjustments(adjustments: list[dict]) -> list[str]:
    """发布前自检：逐张 before+delta=after，返回违例账单号（空列表表示全过）。"""
    broken = []
    for row in adjustments:
        if int(row["before_cents"]) + int(row["delta_cents"]) != int(row["after_cents"]):
            broken.append(row["invoice_id"])
    return broken


def revision_totals(adjustments: list[dict]) -> dict:
    refund = sum(-int(row["delta_cents"]) for row in adjustments
                 if row["delta_cents"] < 0)
    surcharge = sum(int(row["delta_cents"]) for row in adjustments
                    if row["delta_cents"] > 0)
    return {
        "revision_count": len({row["correction_id"] for row in adjustments
                               if row["correction_id"]}),
        "affected_invoices": sum(1 for row in adjustments
                                 if row["delta_cents"] != 0),
        "refund_cents": refund,
        "surcharge_cents": surcharge,
        "net_delta_cents": surcharge - refund,
    }
