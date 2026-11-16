"""M8 不变量：修订守恒、幂等、版本不可变、跨层一致。

固定修订功能的四条硬保证，任何重构都必须继续满足：
1. 原账单版本快照逐字不可变（哈希一致）；
2. 调整差额总和 = 当前账单合计 − 原账单合计；
3. 同更正号重试不新增调整、不新增版本；
4. 账单/调整/导出/清单读同一批表，金额只有一个来源。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from app.modules.m8_revisions import reconcile as revision_reconcile
from app.modules.m8_revisions import service as revision_service

CORRECTION = {
    "correction_id": "COR-INV-1", "sub_id": "SUB-1003",
    "effective": "2026-02-15T00:00:00+08:00", "kind": "降配",
    "new_plan": "基础版", "new_price": "300.00", "reason": "不变量用例",
}


def test_snapshot_hash_immutable_across_revisions(run_id, store, settings):
    original = {row["invoice_id"]: dict(row) for row in store.list_invoices(run_id)}
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-INV-1"], settings)

    snapshot = {row["invoice_id"]: row
                for row in store.list_invoice_versions(run_id, 1)}
    for invoice_id, old in original.items():
        assert snapshot[invoice_id]["row_hash"] == \
            revision_reconcile.invoice_hash(old)
        assert snapshot[invoice_id]["total_cents"] == old["total_cents"]


def test_revision_delta_conservation(run_id, store, settings):
    original_total = sum(row["total_cents"] for row in store.list_invoices(run_id))
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-INV-1"], settings)

    current_total = sum(row["total_cents"] for row in store.list_invoices(run_id))
    delta_sum = sum(row["delta_cents"] for row in store.list_adjustments(run_id))
    assert delta_sum == current_total - original_total
    # 逐张恒等式
    for row in store.list_adjustments(run_id):
        assert row["before_cents"] + row["delta_cents"] == row["after_cents"]


def test_retry_never_creates_second_adjustment(run_id, store, settings):
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-INV-1"], settings)
    adjustments_after_first = len(store.list_adjustments(run_id))
    versions_after_first = len(store.list_invoice_versions(run_id))

    for _ in range(3):
        result = revision_service.publish(store, run_id, ["COR-INV-1"], settings)
        assert result["idempotent"] is True
    assert len(store.list_adjustments(run_id)) == adjustments_after_first
    assert len(store.list_invoice_versions(run_id)) == versions_after_first
    assert store.latest_revision_no(run_id) == 1


def test_export_pages_manifest_share_one_source_of_truth(client):
    client.post("/api/subscriptions/import", json={})
    client.post("/api/revisions/corrections", json={"corrections": [{
        "correction_id": "COR-WEB-1", "sub_id": "SUB-1003",
        "effective": "2026-02-15T00:00:00+08:00", "kind": "降配",
        "new_price": "300.00"}]})
    client.post("/api/revisions/publish", json={"correction_ids": ["COR-WEB-1"]})

    api_total = int(client.get("/api/invoices").json()["totals"]["invoice_cents"])
    bill_rows = list(csv.DictReader(io.StringIO(
        client.get("/api/export/invoices.csv").text.lstrip("﻿"))))
    bill_sum = int(sum((Decimal(row["账单额(元)"]) for row in bill_rows),
                       Decimal("0")) * 100)
    adj_rows = list(csv.DictReader(io.StringIO(
        client.get("/api/revisions/adjustments.csv").text.lstrip("﻿"))))
    # 账单导出与 API 合计一致
    assert bill_sum == api_total
    # 调整单 CSV 每行都能由当前账单与原账单复算
    invoices_now = {row["账单号"]: int(Decimal(row["账单额(元)"]) * 100)
                    for row in bill_rows}
    for row in adj_rows:
        assert int(Decimal(row["修订后(元)"]) * 100) == invoices_now[row["账单号"]]
    # 清单记录修订版，与接口 current_revision 一致
    manifest = client.get("/api/export/manifest.json").json()
    overview = client.get("/api/revisions").json()
    assert manifest["current_revision"] == overview["current_revision"] == 1
    # 五本账全平
    assert all(row["status"] == "ok"
               for row in client.get("/api/reconcile").json()["ledgers"])
