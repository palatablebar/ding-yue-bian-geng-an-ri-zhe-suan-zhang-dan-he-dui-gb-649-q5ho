"""M8 账单修订服务：登记更正 → 原子发布修订版 → 调整单与对账。

四条硬保证：
1. 原账单不可变：发布前把整版账单快照进 invoice_versions，任何时刻都能取回旧版；
2. 幂等：同一更正号重发/重试直接复用已发布结果，绝不重复产生调整；
3. 原子发布：快照、派生表整版替换、调整单在一个事务里，失败整体回滚，
   旧版派生表逐行保留，只把更正单标记 failed 并留失败原因；
4. 一致：账单、修订说明、对账（含修订账）、导出读同一批表，数字只有一个来源。
"""
from __future__ import annotations

import datetime as dt

from ... import config, manifest
from ...errors import Conflict, NotFound
from ..m4_proration import rounding
from ..m6_invoices import invoice as invoice_mod
from ..m6_invoices import reconcile as reconcile_mod
from . import reconcile as revision_reconcile
from . import revision as revision_compute
from .normalize import normalize_many


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _sub_index(store, run_id: str) -> dict[str, dict]:
    return {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}


def _payload_digest(payload: dict) -> str:
    import json

    return manifest.digest_parts([
        json.dumps(payload, ensure_ascii=False, sort_keys=True)])[:24]


def register(store, run_id: str, items: list[dict], settings) -> dict:
    """登记一批更正单；已存在的更正号原样返回，不重复登记。"""
    run = store.get_run(run_id)
    if run is None:
        raise NotFound("批次不存在", code="E_NO_RUN")
    subs = _sub_index(store, run_id)
    rows = normalize_many(items, subs, settings)
    received_at = _now()
    created: list[dict] = []
    for row in rows:
        existing = store.get_correction(run_id, row["change_id"])
        if existing is not None:
            created.append(existing)
            continue
        payload = next(item for item in items
                       if str(item.get("correction_id", "")).strip() == row["change_id"])
        store.insert_correction({
            "run_id": run_id,
            "correction_id": row["change_id"],
            "sub_id": row["sub_id"],
            "effective_date": row["local_date"],
            "kind": row["kind"],
            "new_plan": row["new_plan"],
            "new_price_text": row["new_price_text"],
            "reason": row["reason"],
            "source": row["source"],
            "payload": payload,
            "payload_digest": _payload_digest(payload),
            "status": "received",
            "revision_no": 0,
            "received_at": received_at,
        })
        created.append(store.get_correction(run_id, row["change_id"]))
    return {"received": len(created), "corrections": created}


def _stages(store, run_id: str, revision_no: int, totals: dict,
            correction_ids: list[str]) -> list[dict]:
    stages = store.list_stages(run_id)
    stages.append({
        "stage": "revision",
        "in_count": len(correction_ids),
        "out_count": totals.get("affected_invoices", 0),
        "quarantine_count": 0,
        "status": "done",
        "note": (f"修订 R{revision_no}：更正 {','.join(correction_ids)}，"
                 f"净差额 {totals.get('net_delta_cents', 0)} 分"),
    })
    return stages


def publish(store, run_id: str, correction_ids: list[str], settings) -> dict:
    """发布修订版；全部更正必须处于可发布状态，返回修订摘要。

    已发布的更正再次提交（补发/重试）走幂等分支：返回原修订结果，不再产生调整。
    """
    run = store.get_run(run_id)
    if run is None:
        raise NotFound("批次不存在", code="E_NO_RUN")
    ids = [str(value).strip() for value in correction_ids if str(value).strip()]
    if not ids:
        from ...errors import BadRequest

        raise BadRequest("至少指定一条更正号", code="E_CORRECTION_FIELD")

    corrections = []
    for correction_id in ids:
        row = store.get_correction(run_id, correction_id)
        if row is None:
            raise NotFound(f"更正单不存在：{correction_id}", code="E_NO_CORRECTION")
        corrections.append(row)

    published = [row for row in corrections if row["status"] == "published"]
    pending = [row for row in corrections if row["status"] in ("received", "failed")]
    if published and pending:
        raise Conflict(
            "同一请求里混用了已发布与未发布的更正号；已发布更正不要重试，"
            "新更正请单独发布", code="E_CORRECTION_MIXED")
    if published and not pending:
        return _idempotent_result(store, run_id, published[0]["revision_no"], corrections)

    revision_no = store.latest_revision_no(run_id) + 1
    summary = _compute(store, run_id, pending, settings)
    broken = revision_reconcile.verify_adjustments(summary["adjustments"])
    if broken:
        raise Conflict(f"调整单恒等式校验失败：{','.join(broken)}",
                       code="E_REVISION_UNBALANCED")

    try:
        store.publish_revision(
            run_id, revision_no,
            snapshot=summary["snapshot"], hashes=summary["hashes"],
            changes=summary["merged_changes"],
            segments=summary["segments"], components=summary["components"],
            invoices=summary["invoices"], adjustments=summary["adjustments"],
            ledgers=summary["ledgers"], totals=summary["totals"],
            stages=summary["stages"],
        )
    except Exception as exc:  # noqa: BLE001 - 发布失败留痕，但旧版已由事务回滚保住
        for row in pending:
            store.mark_correction(
                run_id, row["correction_id"], status="failed",
                failure_note=f"{type(exc).__name__}: {exc}"[:500])
        raise

    published_at = _now()
    for row in pending:
        store.mark_correction(run_id, row["correction_id"], status="published",
                              revision_no=revision_no, published_at=published_at)
    _write_manifest(store, run_id, settings, revision_no, summary, pending, published_at)
    return _idempotent_result(store, run_id, revision_no, pending)


def _compute(store, run_id: str, corrections: list[dict], settings) -> dict:
    """读当前已发布版本，在内存里并入更正重算，产出待发布的整版数据。"""
    subs = store.list_subscriptions(run_id)
    base_changes = store.list_changes(run_id)
    snapshot = store.list_invoices(run_id)
    if not snapshot:
        from ...errors import BadRequest

        raise BadRequest("当前批次还没有账单，无法修订", code="E_NO_INVOICE")

    correction_rows = []
    for index, row in enumerate(corrections):
        payload = row["payload"]
        normalized = normalize_many([payload], {s["sub_id"]: s for s in subs},
                                    settings)[0]
        normalized["seq"] = 10_000 + index
        correction_rows.append(normalized)

    computed = revision_compute.compute(subs, base_changes, correction_rows, settings)
    before_invoices = snapshot
    after_invoices = computed["invoices"]
    correction_ids = [row["correction_id"] for row in corrections]
    # 每张账单实际依据哪些更正：按更正单的订阅号映射到账单号
    invoice_id_by_sub = {sub["sub_id"]: invoice_mod.invoice_id_for(sub) for sub in subs}
    corrections_by_invoice: dict[str, list[str]] = {}
    for row in corrections:
        invoice_id = invoice_id_by_sub.get(row["sub_id"], "")
        corrections_by_invoice.setdefault(invoice_id, []).append(row["correction_id"])
    adjustments = revision_reconcile.build_adjustments(
        before_invoices=before_invoices, after_invoices=after_invoices,
        corrections_by_invoice=corrections_by_invoice)
    hashes = {row["invoice_id"]: revision_reconcile.invoice_hash(row)
              for row in snapshot}

    counters = (store.get_run(run_id) or {}).get("counters", {})
    ledgers = reconcile_mod.build(
        counters=counters, invoices=after_invoices,
        segments=computed["segments"], components=computed["components"])
    ledgers.append(revision_reconcile.revision_ledger(
        adjustments, before_invoices, after_invoices))
    for index, row in enumerate(ledgers):
        row["seq"] = index

    totals = reconcile_mod.totals(
        counters, after_invoices, computed["segments"], computed["components"])
    from decimal import Decimal

    totals["segment_exact_total"] = str(sum(
        (Decimal(row["amount_exact"]) for row in computed["segments"]), Decimal("0")))
    totals.update(revision_reconcile.revision_totals(adjustments))
    new_revision_no = store.latest_revision_no(run_id) + 1
    totals["current_revision"] = new_revision_no

    stages = _stages(store, run_id, new_revision_no, totals, correction_ids)
    return {
        "snapshot": snapshot,
        "hashes": hashes,
        "segments": computed["segments"],
        "components": computed["components"],
        "invoices": after_invoices,
        "adjustments": adjustments,
        "ledgers": ledgers,
        "totals": totals,
        "stages": stages,
        "merged_changes": computed["changes"],
        "correction_ids": correction_ids,
    }


def _idempotent_result(store, run_id: str, revision_no: int,
                       corrections: list[dict]) -> dict:
    adjustments = store.list_adjustments(run_id, revision_no)
    return {
        "run_id": run_id,
        "revision_no": revision_no,
        "idempotent": True,
        "correction_ids": [row["correction_id"] for row in corrections],
        "adjustments": adjustments,
        "revision_totals": revision_reconcile.revision_totals(adjustments),
        "ledgers": store.list_reconciliations(run_id),
        "totals": store.list_totals(run_id),
    }


def _write_manifest(store, run_id: str, settings, revision_no: int,
                    summary: dict, corrections: list[dict], published_at: str) -> None:
    data = store.get_manifest(run_id) or {}
    revisions = list(data.get("revisions", []))
    revisions.append({
        "revision_no": revision_no,
        "published_at": published_at,
        "correction_ids": [row["correction_id"] for row in corrections],
        "reasons": {row["correction_id"]: row.get("reason", "") for row in corrections},
        "payload_digests": {row["correction_id"]: row["payload_digest"]
                            for row in corrections},
        "affected_invoices": summary["totals"]["affected_invoices"],
        "refund_cents": summary["totals"]["refund_cents"],
        "surcharge_cents": summary["totals"]["surcharge_cents"],
        "net_delta_cents": summary["totals"]["net_delta_cents"],
        "snapshot_hashes": summary["hashes"],
    })
    data["revisions"] = revisions
    data["current_revision"] = revision_no
    store.put_manifest(run_id, data)


# ---------- 只读视图 ----------
def overview(store, run_id: str) -> dict:
    current_no = store.latest_revision_no(run_id)
    corrections = store.list_corrections(run_id)
    adjustments = store.list_adjustments(run_id)
    versions = store.list_invoice_versions(run_id)
    return {
        "current_revision": current_no,
        "corrections": corrections,
        "adjustments": adjustments,
        "revision_totals": revision_reconcile.revision_totals(adjustments),
        "versions": [
            {"revision_no": row["revision_no"], "invoice_id": row["invoice_id"],
             "total_cents": row["total_cents"], "row_hash": row["row_hash"]}
            for row in versions
        ],
    }


def correction_rows(store, run_id: str) -> list[dict]:
    rows = store.list_corrections(run_id)
    subs = _sub_index(store, run_id)
    out = []
    for row in rows:
        sub = subs.get(row["sub_id"], {})
        out.append({
            **row,
            "account": sub.get("account", ""),
            "kind_label": config.KIND_LABELS.get(row["kind"], row["kind"]),
        })
    return out


def adjustment_rows(store, run_id: str) -> list[dict]:
    rows = store.list_adjustments(run_id)
    out = []
    for row in rows:
        out.append({
            **row,
            "before": rounding.cents_text(row["before_cents"]),
            "after": rounding.cents_text(row["after_cents"]),
            "delta": rounding.cents_text(abs(row["delta_cents"])),
            "direction_label": {"refund": "应退", "surcharge": "应补",
                                "unchanged": "不变"}.get(row["direction"],
                                                        row["direction"]),
        })
    return out
