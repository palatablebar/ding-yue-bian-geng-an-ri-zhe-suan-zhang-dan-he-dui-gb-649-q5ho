"""M8 路由：登记/发布/幂等/校验错误/页面/导出一致。"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import expected


def _correction(cid="COR-API-1", sub_id="SUB-1003", price="300.00", **over):
    payload = {
        "correction_id": cid, "sub_id": sub_id,
        "effective": "2026-02-15T00:00:00+08:00", "kind": "降配",
        "new_plan": "基础版", "new_price": price,
        "reason": "合同更正", "source": "ticket-9",
    }
    payload.update(over)
    return payload


def _import(client):
    assert client.post("/api/subscriptions/import", json={}).status_code == 200


def test_register_and_publish_flow(seeded_client):
    client = seeded_client
    r = client.post("/api/revisions/corrections",
                    json={"corrections": [_correction()]})
    assert r.status_code == 200
    assert r.json()["received"] == 1

    r = client.post("/api/revisions/publish",
                    json={"correction_ids": ["COR-API-1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["revision_no"] == 1
    assert body["revision_totals"]["refund_cents"] == 15000
    # 五本账全平
    assert all(row["status"] == "ok" for row in body["ledgers"])


def test_retry_same_correction_is_idempotent(seeded_client):
    client = seeded_client
    client.post("/api/revisions/corrections", json={"corrections": [_correction()]})
    first = client.post("/api/revisions/publish",
                        json={"correction_ids": ["COR-API-1"]}).json()
    second = client.post("/api/revisions/publish",
                         json={"correction_ids": ["COR-API-1"]}).json()
    assert first["revision_no"] == second["revision_no"] == 1
    assert second["idempotent"] is True
    # 重发整条更正（再登记再发布）同样不重复
    again = client.post("/api/revisions/corrections",
                        json={"corrections": [_correction()]})
    assert again.json()["received"] == 1
    third = client.post("/api/revisions/publish",
                        json={"correction_ids": ["COR-API-1"]}).json()
    assert third["revision_no"] == 1


def test_publish_all_pending_without_ids(seeded_client):
    client = seeded_client
    client.post("/api/revisions/corrections", json={"corrections": [_correction()]})
    r = client.post("/api/revisions/publish", json={})
    assert r.status_code == 200
    assert r.json()["correction_ids"] == ["COR-API-1"]


def test_validation_errors(seeded_client):
    client = seeded_client
    # 账期外
    out = client.post("/api/revisions/corrections", json={"corrections": [_correction(
        cid="COR-OUT", effective="2026-04-01T00:00:00+08:00")]})
    assert out.status_code == 422
    assert out.json()["error"] == "E_CORRECTION_OUT_OF_PERIOD"
    # 未知订阅
    unknown = client.post("/api/revisions/corrections", json={"corrections": [_correction(
        cid="COR-UNK", sub_id="SUB-4040")]})
    assert unknown.status_code == 422
    assert unknown.json()["error"] == "E_CORRECTION_UNKNOWN_SUB"
    # 坏类型 / 坏价格 / 缺字段
    assert client.post("/api/revisions/corrections", json={"corrections": [_correction(
        cid="COR-K", kind="飞天")]}).status_code == 422
    assert client.post("/api/revisions/corrections", json={"corrections": [_correction(
        cid="COR-P", new_price="abc")]}).status_code == 422
    assert client.post("/api/revisions/corrections",
                       json={"corrections": [{"sub_id": "SUB-1003"}]}).status_code == 400
    # 什么都没写
    assert client.post("/api/revisions/corrections", json={}).status_code == 400


def test_duplicate_correction_in_batch_rejected(seeded_client):
    client = seeded_client
    r = client.post("/api/revisions/corrections", json={"corrections": [
        _correction(cid="COR-DUP"), _correction(cid="COR-DUP")]})
    assert r.status_code == 422
    assert r.json()["error"] == "E_CORRECTION_DUP"


def test_publish_unknown_correction_404(seeded_client):
    client = seeded_client
    r = client.post("/api/revisions/publish",
                    json={"correction_ids": ["GHOST"]})
    assert r.status_code == 404
    assert r.json()["error"] == "E_NO_CORRECTION"


def test_adjustment_csv_matches_api(seeded_client):
    client = seeded_client
    client.post("/api/revisions/corrections", json={"corrections": [_correction()]})
    client.post("/api/revisions/publish", json={"correction_ids": ["COR-API-1"]})

    text = client.get("/api/revisions/adjustments.csv").text
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    changed = [row for row in rows if row["方向"] != "不变"]
    assert len(changed) == 1
    row = changed[0]
    assert row["账单号"] == "INV-202602-SUB-1003"
    assert row["原账单额(元)"] == "600.00"
    assert row["修订后(元)"] == "450.00"
    assert row["差额绝对值(元)"] == "150.00"
    assert row["方向"] == "应退"
    # 每行满足 原额 + 带符号差额 = 新额
    for item in rows:
        before = Decimal(item["原账单额(元)"])
        after = Decimal(item["修订后(元)"])
        delta = Decimal(item["差额带符号(元)"])
        assert before + delta == after


def test_bill_export_and_pages_stay_consistent(seeded_client):
    client = seeded_client
    client.post("/api/revisions/corrections", json={"corrections": [_correction()]})
    client.post("/api/revisions/publish", json={"correction_ids": ["COR-API-1"]})

    invoices = client.get("/api/invoices").json()
    csv_rows = list(csv.DictReader(io.StringIO(
        client.get("/api/export/invoices.csv").text.lstrip("﻿"))))
    csv_sum = sum((Decimal(row["账单额(元)"]) for row in csv_rows), Decimal("0"))
    assert csv_sum == Decimal(invoices["totals"]["invoice_cents"]) / 100
    # 行数不变（修订只改金额，不增删账单）
    assert len(csv_rows) == expected.INVOICE_COUNT
    # 清单带上修订块
    manifest = client.get("/api/export/manifest.json").json()
    assert manifest["current_revision"] == 1
    assert manifest["revisions"][0]["correction_ids"] == ["COR-API-1"]
    # 修订页与账单页都能打开，且能看到应退与版本快照
    revision_page = client.get("/revisions").text
    assert "账单修订" in revision_page and "应退" in revision_page
    invoice_page = client.get("/invoices").text
    assert "修订账" in invoice_page
    # 首页对账也展示修订账
    assert "修订账" in client.get("/").text


def test_overview_lists_corrections_and_versions(seeded_client):
    client = seeded_client
    client.post("/api/revisions/corrections", json={"corrections": [_correction()]})
    client.post("/api/revisions/publish", json={"correction_ids": ["COR-API-1"]})
    data = client.get("/api/revisions").json()
    assert data["current_revision"] == 1
    assert data["corrections"][0]["status"] == "published"
    assert len(data["versions"]) == expected.INVOICE_COUNT
    assert data["revision_totals"]["affected_invoices"] == 1
