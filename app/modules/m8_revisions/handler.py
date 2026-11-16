"""M8 路由：更正登记、修订发布、修订视图与调整单导出。

发布是显式两步：先 POST /corrections 登记留痕，再 POST /revisions/publish 发布，
复核人可以在登记后、发布前看清依据；补发/重试同一条更正走幂等分支。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request, Response

from ... import web
from ...errors import BadRequest
from . import csv_out, service

router = APIRouter(prefix="/api/revisions", tags=["M8 账单修订"])


def _correction_items(payload: dict | None) -> list[dict]:
    body = payload or {}
    items = body.get("corrections")
    if items is None:
        # 允许单条对象，便于手工补录
        if "correction_id" in body:
            items = [body]
        else:
            raise BadRequest("请求体需要 corrections 数组", code="E_CORRECTION_FIELD")
    if not isinstance(items, list):
        raise BadRequest("corrections 必须是数组", code="E_CORRECTION_FIELD")
    return items


@router.post("/corrections")
def register(request: Request, payload: dict[str, Any] | None = Body(default=None)) -> dict:
    run = web.run_or_404(request)
    items = _correction_items(payload)
    data = service.register(request.app.state.store, run["run_id"], items,
                            request.app.state.settings)
    return {"run_id": run["run_id"], **data}


@router.post("/publish")
def publish(request: Request, payload: dict[str, Any] | None = Body(default=None)) -> dict:
    run = web.run_or_404(request)
    body = payload or {}
    ids = body.get("correction_ids")
    if ids is None:
        # 没给 id 就发布当前所有未发布更正
        pending = [row for row in request.app.state.store.list_corrections(run["run_id"])
                   if row["status"] in ("received", "failed")]
        ids = [row["correction_id"] for row in pending]
    if not isinstance(ids, list):
        raise BadRequest("correction_ids 必须是数组", code="E_CORRECTION_FIELD")
    return service.publish(request.app.state.store, run["run_id"], ids,
                           request.app.state.settings)


@router.get("")
def overview(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    return {"run_id": run["run_id"], **service.overview(store, run["run_id"])}


@router.get("/corrections")
def corrections(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    return {"run_id": run["run_id"],
            "corrections": service.correction_rows(store, run["run_id"])}


@router.get("/adjustments")
def adjustments(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    return {"run_id": run["run_id"],
            "adjustments": service.adjustment_rows(store, run["run_id"])}


@router.get("/adjustments.csv")
def adjustments_csv(request: Request) -> Response:
    run = web.run_or_404(request)
    store = request.app.state.store
    rows = service.adjustment_rows(store, run["run_id"])
    text = csv_out.adjustments_csv(rows)
    filename = f"adjustments-{run['run_id']}.csv"
    return Response(
        content=csv_out.with_bom(text), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Row-Count": str(len(rows))},
    )
