"""M8 修订重算（纯计算）：更正并入后的分段、金额与受影响账单。

口径必须与 M4/M5/M6 完全一致：同样的变更走采信流程和走修订流程，
逐分段、逐抵扣行、逐账单金额相同。
"""
from __future__ import annotations

from decimal import Decimal

from app.modules.m5_changes import segments as segment_mod
from app.modules.m6_invoices import invoice as invoice_mod
from app.modules.m8_revisions import revision as revision_compute


def _sub(store, run_id, sub_id):
    return next(row for row in store.list_subscriptions(run_id)
                if row["sub_id"] == sub_id)


def _correction_change(correction_id, sub_id, local_date, kind, new_price,
                       *, seq=10000):
    """构造与 changes 表同构的更正变更行（归一由 normalize 负责，这里直接给结果）。"""
    return {
        "seq": seq, "change_id": correction_id, "sub_id": sub_id,
        "effective_utc": f"{local_date}T00:00:00+00:00",
        "effective_local": f"{local_date} 00:00:00", "local_date": local_date,
        "kind": kind, "new_plan": "基础版", "new_price_text": new_price,
        "new_price": new_price, "source_file": "correction", "row_no": 0,
        "applied": 1, "superseded_by": "",
        "flags": ["R_REVISION_LATE", "R_REVISION_CORRECTION"],
    }


def test_late_downgrade_refund_matches_proration(run_id, store, settings):
    # SUB-1003：整期 600 元，2/15 起降配到 300 → 600×14/28 + 300×14/28 = 450.00
    sub = _sub(store, run_id, "SUB-1003")
    before = store.get_invoice(run_id, invoice_mod.invoice_id_for(sub))
    assert before["total_cents"] == 60000

    correction = _correction_change("COR-1", "SUB-1003", "2026-02-15",
                                    "downgrade", "300.00")
    result = revision_compute.compute(
        store.list_subscriptions(run_id), store.list_changes(run_id),
        [correction], settings)

    after = next(row for row in result["invoices"]
                 if row["invoice_id"] == before["invoice_id"])
    assert after["total_cents"] == 45000
    # 账单级带上修订标记，复核人一眼看出这张账单被更正过
    assert "R_REVISION_CORRECTION" in after["flags"]
    # 守恒：分段分合计 == 账单额
    seg_sum = sum(row["amount_cents"] for row in result["segments"]
                  if row["invoice_id"] == before["invoice_id"])
    assert seg_sum == after["total_cents"]


def test_revision_equivalent_to_ingesting_change(run_id, store, settings):
    # 同一变更走修订并入，与直接放进变更序列重建，分段结果逐字一致。
    # 选一个原本完全没有变更的订阅，避免同日替代逻辑干扰对比。
    sub = _sub(store, run_id, "SUB-1009")
    assert not [row for row in store.list_changes(run_id)
                if row["sub_id"] == "SUB-1009"]
    new_day = "2026-02-21"
    correction = _correction_change("COR-2", "SUB-1009", new_day,
                                    "downgrade", "300.00")
    revised = revision_compute.compute(
        store.list_subscriptions(run_id), store.list_changes(run_id),
        [correction], settings)
    own_changes = [row for row in store.list_changes(run_id)
                   if row["sub_id"] == "SUB-1009"]
    direct = segment_mod.build_segments(sub, own_changes + [correction])

    revised_segments = [row for row in revised["segments"]
                        if row["sub_id"] == "SUB-1009"]
    assert [(row["start_date"], row["end_date"], row["days"], row["monthly"])
            for row in revised_segments] == [
        (seg.start.isoformat(), seg.end.isoformat(), seg.days, str(seg.monthly))
        for seg in segment_mod.billable_segments(direct)]


def test_correction_replaces_same_day_original_change(run_id, store, settings):
    # SUB-1001 在 2/3、2/11 已有两条生效变更；更正落在已有变更日 2/11，
    # 必须替代原生效条而非并存（同日只允许一条生效）
    original = [row for row in store.list_changes(run_id)
                if row["sub_id"] == "SUB-1001"]
    assert any(row["local_date"] == "2026-02-11" and row["applied"]
               for row in original)
    correction = _correction_change("COR-3", "SUB-1001", "2026-02-11",
                                    "downgrade", "500.00")
    merged = revision_compute.merge_changes(original, [correction])
    same_day = [row for row in merged if row["local_date"] == "2026-02-11"]
    applied = [row for row in same_day if row["applied"]]
    assert len(applied) == 1
    assert applied[0]["change_id"] == "COR-3"
    replaced = [row for row in same_day if not row["applied"]]
    assert all(row["superseded_by"] == "COR-3" for row in replaced)
    # 原行证据保留，没有物理删除
    assert "CHG-0001" in {row["change_id"] for row in same_day}


def test_unrelated_subscriptions_unchanged(run_id, store, settings):
    # 只改 SUB-1003，其余账单逐分不变
    before = {row["invoice_id"]: row["total_cents"]
              for row in store.list_invoices(run_id)}
    correction = _correction_change("COR-4", "SUB-1003", "2026-02-10",
                                    "downgrade", "300.00")
    result = revision_compute.compute(
        store.list_subscriptions(run_id), store.list_changes(run_id),
        [correction], settings)
    after = {row["invoice_id"]: row["total_cents"] for row in result["invoices"]}
    changed = [invoice_id for invoice_id in before if before[invoice_id] != after[invoice_id]]
    assert changed == ["INV-202602-SUB-1003"]


def test_decimal_never_float(run_id, store, settings):
    correction = _correction_change("COR-5", "SUB-1003", "2026-02-15",
                                    "downgrade", "300.00")
    result = revision_compute.compute(
        store.list_subscriptions(run_id), store.list_changes(run_id),
        [correction], settings)
    # 精确额是 Decimal 字符串，分是整数，全程没有 float
    for row in result["segments"]:
        Decimal(row["amount_exact"])
        assert isinstance(row["amount_cents"], int)
    for row in result["invoices"]:
        assert isinstance(row["total_cents"], int)
