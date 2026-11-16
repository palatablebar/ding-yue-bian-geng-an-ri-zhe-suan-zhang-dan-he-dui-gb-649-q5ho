"""M8 修订服务：原账单留档、幂等、原子回滚、修订账守恒。

直接打服务层（不经 HTTP），便于注入发布失败，验证「失败恢复完整旧版」。
"""
from __future__ import annotations

import pytest

from app.modules.m6_invoices import invoice as invoice_mod
from app.modules.m8_revisions import reconcile as revision_reconcile
from app.modules.m8_revisions import service as revision_service

CORRECTION = {
    "correction_id": "COR-SVC-1", "sub_id": "SUB-1003",
    "effective": "2026-02-15T00:00:00+08:00", "kind": "降配",
    "new_plan": "基础版", "new_price": "300.00",
    "reason": "合同更正", "source": "ticket-1",
}


def _invoice(store, run_id, sub_id):
    sub = next(s for s in store.list_subscriptions(run_id) if s["sub_id"] == sub_id)
    return store.get_invoice(run_id, invoice_mod.invoice_id_for(sub))


def test_publish_keeps_original_version_snapshot(run_id, store, settings):
    before = _invoice(store, run_id, "SUB-1003")
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-SVC-1"], settings)

    # 当前账单已变，但 R1 版本快照逐字保留原账单（含哈希）
    after = _invoice(store, run_id, "SUB-1003")
    assert after["total_cents"] == 45000
    snapshot = store.list_invoice_versions(run_id, 1)
    saved = next(row for row in snapshot
                 if row["invoice_id"] == before["invoice_id"])
    assert saved["total_cents"] == 60000
    assert saved["row_hash"] == revision_reconcile.invoice_hash(before)
    # 快照覆盖整版全部账单，不止受影响那张
    assert len(snapshot) == len(store.list_invoices(run_id))


def test_idempotent_retry_does_not_duplicate_adjustment(run_id, store, settings):
    revision_service.register(store, run_id, [CORRECTION], settings)
    first = revision_service.publish(store, run_id, ["COR-SVC-1"], settings)
    second = revision_service.publish(store, run_id, ["COR-SVC-1"], settings)

    assert first["revision_no"] == second["revision_no"] == 1
    assert second["idempotent"] is True
    # 调整单、版本快照都只有一份，没有重复产生
    assert len(store.list_adjustments(run_id)) == len(store.list_invoices(run_id))
    assert {row["revision_no"] for row in store.list_invoice_versions(run_id)} == {1}
    correction = store.get_correction(run_id, "COR-SVC-1")
    assert correction["status"] == "published"


def test_re_register_same_correction_is_idempotent(run_id, store, settings):
    first = revision_service.register(store, run_id, [CORRECTION], settings)
    second = revision_service.register(store, run_id, [dict(CORRECTION)], settings)
    assert first["received"] == second["received"] == 1
    assert len(store.list_corrections(run_id)) == 1


def test_publish_failure_rolls_back_complete_old_version(run_id, store, settings,
                                                         monkeypatch):
    before_invoices = [(row["invoice_id"], row["total_cents"])
                       for row in store.list_invoices(run_id)]
    before_segments = len(store.list_segments(run_id))
    revision_service.register(store, run_id, [CORRECTION], settings)

    # 在事务内部注入失败：包装事务连接，写调整单这一步炸掉（前面已写快照/删派生表）
    real_transaction = type(store).transaction

    class BoomError(RuntimeError):
        pass

    def flaky_transaction(self):
        ctx = real_transaction(self)
        conn = ctx.__enter__()
        real_execute = conn.execute
        real_executemany = conn.executemany

        def flaky_execute(sql, params=()):
            if "INSERT INTO revision_adjustments" in sql:
                raise BoomError("模拟发布过程中断电/磁盘错误")
            return real_execute(sql, params)

        def flaky_executemany(sql, parameters):
            if "INSERT INTO revision_adjustments" in sql:
                raise BoomError("模拟发布过程中断电/磁盘错误")
            return real_executemany(sql, parameters)

        class ConnProxy:
            """只拦截写库入口，其余（含 commit/rollback/close）透传真连接。"""

            def __getattr__(self, name):
                if name == "execute":
                    return flaky_execute
                if name == "executemany":
                    return flaky_executemany
                return getattr(conn, name)

        from contextlib import contextmanager

        @contextmanager
        def holder():
            proxy = ConnProxy()
            try:
                yield proxy
            except BaseException as exc:
                ctx.__exit__(type(exc), exc, exc.__traceback__)
                raise
            else:
                ctx.__exit__(None, None, None)

        return holder()

    monkeypatch.setattr(type(store), "transaction", flaky_transaction)
    with pytest.raises(BoomError):
        revision_service.publish(store, run_id, ["COR-SVC-1"], settings)
    monkeypatch.setattr(type(store), "transaction", real_transaction)

    # 旧版派生表逐行恢复：账单额、分段数、对账、版本号全部回到发布前
    after_invoices = [(row["invoice_id"], row["total_cents"])
                      for row in store.list_invoices(run_id)]
    assert after_invoices == before_invoices
    assert len(store.list_segments(run_id)) == before_segments
    assert store.latest_revision_no(run_id) == 0
    assert store.list_adjustments(run_id) == []
    ledgers = store.list_reconciliations(run_id)
    assert all(row["status"] == "ok" for row in ledgers)
    assert all(row["ledger"] != "revision" for row in ledgers)
    # 更正单标记 failed 并留原因，重试仍可成功（不卡死）
    correction = store.get_correction(run_id, "COR-SVC-1")
    assert correction["status"] == "failed"
    assert "模拟发布过程中断电" in correction["failure_note"]

    retry = revision_service.publish(store, run_id, ["COR-SVC-1"], settings)
    assert retry["revision_no"] == 1
    assert _invoice(store, run_id, "SUB-1003")["total_cents"] == 45000


def test_revision_ledger_balances(run_id, store, settings):
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-SVC-1"], settings)
    ledgers = store.list_reconciliations(run_id)
    statuses = {row["ledger"]: row["status"] for row in ledgers}
    # 原四本账依旧平，新增的修订账也平
    assert statuses == {"rows": "ok", "days": "ok", "amount": "ok",
                        "invoices": "ok", "revision": "ok"}
    revision_ledger = next(row for row in ledgers if row["ledger"] == "revision")
    assert int(revision_ledger["left_text"]) == -15000
    assert int(revision_ledger["right_text"]) == -15000


def test_second_revision_chains_versions(run_id, store, settings):
    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-SVC-1"], settings)

    second = {
        "correction_id": "COR-SVC-2", "sub_id": "SUB-1003",
        "effective": "2026-02-20T00:00:00+08:00", "kind": "降配",
        "new_plan": "基础版", "new_price": "150.00", "reason": "二次更正",
    }
    revision_service.register(store, run_id, [second], settings)
    result = revision_service.publish(store, run_id, ["COR-SVC-2"], settings)
    assert result["revision_no"] == 2

    # R1 快照是原始 60000，R2 快照是第一次修订后的 45000，当前是第二次修订结果
    r1 = next(row for row in store.list_invoice_versions(run_id, 1)
              if row["invoice_id"] == "INV-202602-SUB-1003")
    r2 = next(row for row in store.list_invoice_versions(run_id, 2)
              if row["invoice_id"] == "INV-202602-SUB-1003")
    assert (r1["total_cents"], r2["total_cents"]) == (60000, 45000)
    current = _invoice(store, run_id, "SUB-1003")["total_cents"]
    # 链式并入两条更正：600×14/28 + 300×5/28 + 150×9/28
    # 精确合计 401.7857… → 总额四舍五入 401.79，尾差按最大余额法分到分 → 40179
    assert current == 40179
    # 第二次调整单的 before 必须等于第一次修订后的金额，链条不断
    adj = next(row for row in store.list_adjustments(run_id, 2)
               if row["invoice_id"] == "INV-202602-SUB-1003")
    assert adj["before_cents"] == 45000
    assert adj["after_cents"] == current


def test_cannot_publish_unknown_or_mixed_corrections(run_id, store, settings):
    from app.errors import Conflict, NotFound

    revision_service.register(store, run_id, [CORRECTION], settings)
    revision_service.publish(store, run_id, ["COR-SVC-1"], settings)
    revision_service.register(store, run_id, [{
        "correction_id": "COR-SVC-3", "sub_id": "SUB-1002",
        "effective": "2026-02-10T00:00:00Z", "kind": "暂停"}], settings)

    with pytest.raises(NotFound):
        revision_service.publish(store, run_id, ["COR-NOPE"], settings)
    with pytest.raises(Conflict):
        revision_service.publish(store, run_id,
                                 ["COR-SVC-1", "COR-SVC-3"], settings)
