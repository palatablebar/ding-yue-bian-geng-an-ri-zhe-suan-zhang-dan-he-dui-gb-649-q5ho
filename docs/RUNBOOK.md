# 运行手册 subbill

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18208/api/health
```

宿主机端口 18208 → 容器 8000。数据库在命名卷 `subbill-data` 的 `subbill.sqlite`。

## 主流程

```bash
# 1 采信自带脏样例（订阅 + 变更），返回 run_id 与四本账
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' -d '{}'

# 2 周期诊断
curl -s http://127.0.0.1:18208/api/calendar | head -c 400

# 3 隔离区与原因码
curl -s http://127.0.0.1:18208/api/quarantine | head -c 400
curl -s -o /tmp/quarantine.csv http://127.0.0.1:18208/api/quarantine.csv

# 4 折算与变更
curl -s http://127.0.0.1:18208/api/proration | head -c 300
curl -s -X POST http://127.0.0.1:18208/api/changes | head -c 300

# 5 账单与对账
curl -s http://127.0.0.1:18208/api/invoices | head -c 400
curl -s http://127.0.0.1:18208/api/reconcile

# 6 导出
curl -s -o /tmp/invoices.csv http://127.0.0.1:18208/api/export/invoices.csv
curl -s -o /tmp/manifest.json http://127.0.0.1:18208/api/export/manifest.json
```

## 换成自己的数据

样例目录挂载在容器 `/app/sample_data`，采信时传文件名即可（只接受纯文件名）：

```bash
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' \
  -d '{"subscriptions":"subscriptions_gbk.csv","changes":"changes_dirty.csv"}'
```

两份文件的列名会被映射到规范字段：订阅表需要 `订阅号`、`月价(元)`、`期间起`、`期间止`、`状态`；变更表需要 `变更号`、`订阅号`、`生效时间`、`类型`。缺必需列会整体隔离并给出 `R_HEADER_MISSING`。

## 重算与幂等

- 同一份输入再采信一次：命中 `batch_key`，直接返回原批次，行数与合计不变。
- 只重算金额：`POST /api/proration`；重算分段与抵扣：`POST /api/changes`。两者都会重新出账并刷新四本账。
- 想强制新建批次：改任一输入文件的内容（`batch_key` 由输入 sha256 决定）。

## 重建数据库

```bash
docker compose down -v          # 删掉命名卷，下次启动会重建空库
docker compose up -d --build
```

## 账单修订（发账后收到变更更正）

原账单不覆盖，按「登记更正 → 发布修订 → 调整单/导出」两步走，全过程留痕：

```bash
# 1 登记更正单（可多条；correction_id 是幂等键，重发不重复入账）
curl -s -X POST http://127.0.0.1:18208/api/revisions/corrections \
  -H 'Content-Type: application/json' -d '{
    "corrections": [{
      "correction_id": "COR-0001",
      "sub_id": "SUB-1003",
      "effective": "2026-02-15T00:00:00+08:00",
      "kind": "降配",
      "new_plan": "基础版",
      "new_price": "300.00",
      "reason": "客户合同更正，2月15日起降配",
      "source": "ticket-42"
    }]}'

# 2 发布修订版（不传 correction_ids 则发布所有未发布更正）
curl -s -X POST http://127.0.0.1:18208/api/revisions/publish \
  -H 'Content-Type: application/json' -d '{"correction_ids": ["COR-0001"]}'

# 3 复核：更正单、调整单、版本快照
curl -s http://127.0.0.1:18208/api/revisions | head -c 600
curl -s -o /tmp/adjustments.csv http://127.0.0.1:18208/api/revisions/adjustments.csv
```

复核要点：

- 账单页顶部出现「修订提示 R<n>」，对账多一本「修订账」，五本账全平才算发布成功。
- 调整单逐张满足 `原账单额 + 差额 = 修订后金额`；负数差额是应退，正数是应补。
- `/revisions` 页可看每张账单的历史版本快照（含全字段哈希），原账单金额永久可查。
- 补发或重试同一条更正：返回 `idempotent=true` 且 `revision_no` 不变，不产生第二笔调整。
- 发布过程失败：事务整体回滚，账单/分段/对账恢复到发布前；更正单状态变 `failed` 并记录原因，修复后用同一 `correction_id` 重新发布即可。
- 校验错误：账期外 `E_CORRECTION_OUT_OF_PERIOD`、未知订阅 `E_CORRECTION_UNKNOWN_SUB`、坏类型/价格 `E_CORRECTION_KIND/PRICE`、同批重复 `E_CORRECTION_DUP`、混用已发布与未发布 `E_CORRECTION_MIXED`。

## 排错

| 现象 | 检查 |
|---|---|
| 首页 200 但表格为空 | 还没有批次，点「采信」；或 `/api/runs` 看是否有 `done` 批次 |
| 采信返回 400 `E_FILE_MISSING` | 文件名不在 `sample_data/`；只接受纯文件名 |
| 采信返回 400 `E_BAD_FILE_NAME` | 传了路径分隔符或空串 |
| 账单为 0 | 期间内全是暂停/试用，或月价为 0；看账单标记 `R_INVOICE_ZERO` |
| 对账不平 | 看 `/api/reconcile` 哪一本账 `status=mismatch`，再看对应明细 |
| 导出 404 `E_NO_RUN` | 还没批次；先采信 |
| 容器内 pytest 失败 | `docker compose exec -T app python -m pytest -q`，先看第一个失败 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
docker compose exec -T app python -m pytest tests/test_invariants.py -q
```

## 日志

```bash
docker compose logs -f app
docker compose exec -T app ls -l /data
```
