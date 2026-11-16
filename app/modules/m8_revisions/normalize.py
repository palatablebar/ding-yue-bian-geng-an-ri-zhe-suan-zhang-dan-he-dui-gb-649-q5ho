"""M8 更正归一：把外部更正单校验、归一成与 M3 同构的变更行。

更正单是账单发出后才到的「变更更正」，归一规则与采信期变更保持一致：
- 生效时刻按订阅时区定位到本地日，且必须落在账期内（否则不产生调整）；
- 类型/价格走 M3 同一套纯函数，原因码唯一来自 reasons.py；
- 只校验归一，不落库；幂等与发布由 service 负责。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ... import config
from ...errors import BadRequest, Unprocessable
from ..m2_calendar import service as calendar_service
from ..m3_quality import rules

REQUIRED_FIELDS = ("correction_id", "sub_id", "effective", "kind")


def _require(payload: dict, field: str) -> str:
    value = payload.get(field)
    if value is None or not str(value).strip():
        raise BadRequest(f"更正单缺少必填字段：{field}", code="E_CORRECTION_FIELD")
    return str(value).strip()


def normalize(payload: dict, sub: dict, settings, *, seq: int = 0) -> dict:
    """单条更正 → 变更行（结构对齐 changes 表）；不可用直接抛 4xx，不进隔离区。"""
    for field in REQUIRED_FIELDS:
        _require(payload, field)
    correction_id = _require(payload, "correction_id")
    sub_id = _require(payload, "sub_id")
    kind_parsed = rules.normalize_kind(payload.get("kind"))
    if kind_parsed.code:
        raise Unprocessable(f"更正类型无法识别：{payload.get('kind')}",
                            code="E_CORRECTION_KIND")
    kind = kind_parsed.value
    price_parsed = rules.change_price(kind, payload.get("new_price", ""))
    if price_parsed.code:
        raise Unprocessable(f"更正月价无法解析：{payload.get('new_price')}",
                            code="E_CORRECTION_PRICE")

    instant_parsed = rules.parse_instant(payload.get("effective"))
    if instant_parsed.code:
        raise Unprocessable(f"更正生效时间无法解析：{payload.get('effective')}",
                            code="E_CORRECTION_DATE")
    zone = rules.parse_zone(sub["tz"]).value or ZoneInfo(settings.display_tz)
    instant = instant_parsed.value
    if instant.offset is not None:
        utc = instant.naive.replace(tzinfo=instant.offset).astimezone(config.UTC)
    else:
        zone = (rules.parse_zone(instant.zone_name).value if instant.zone_name else zone) \
            or zone
        utc, _dst = calendar_service.resolve_effective(instant.naive, zone)
    local_dt = utc.astimezone(zone)
    local_day = local_dt.date()

    start = dt.date.fromisoformat(sub["period_start"])
    end = dt.date.fromisoformat(sub["period_end"])
    if not (start <= local_day < end):
        raise Unprocessable(
            f"更正生效日 {local_day.isoformat()} 不在账期 "
            f"[{sub['period_start']}, {sub['period_end']}) 内",
            code="E_CORRECTION_OUT_OF_PERIOD",
            detail={"effective_date": local_day.isoformat(),
                    "period_start": sub["period_start"],
                    "period_end": sub["period_end"]})

    flags = ["R_REVISION_LATE", "R_REVISION_CORRECTION", *list(price_parsed.flags)]
    if local_dt.time() != dt.time(0, 0):
        flags.append("R_TIME_TRUNCATED")
    if local_day == start:
        flags.append("R_CHANGE_ON_PERIOD_START")

    return {
        "seq": int(seq),
        "change_id": correction_id,
        "sub_id": sub_id,
        "effective_utc": utc.isoformat(),
        "effective_local": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "local_date": local_day.isoformat(),
        "kind": kind,
        "new_plan": str(payload.get("new_plan", "") or "").strip(),
        "new_price_text": str(payload.get("new_price", "") or "").strip(),
        "new_price": price_parsed.value if price_parsed.value is not None else "",
        "source_file": "correction",
        "row_no": 0,
        "applied": 1,
        "superseded_by": "",
        "flags": flags,
        "correction_id": correction_id,
        "reason": str(payload.get("reason", "") or "").strip(),
        "source": str(payload.get("source", "") or "").strip(),
    }


def normalize_many(items: list[dict], subs: dict[str, dict], settings) -> list[dict]:
    """批量归一；引用未知订阅直接拒绝（更正没有隔离区，拒绝才不会悄悄漏调）。"""
    if not isinstance(items, list) or not items:
        raise BadRequest("corrections 必须是非空数组", code="E_CORRECTION_FIELD")
    rows: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BadRequest(f"第 {index + 1} 条更正不是对象", code="E_CORRECTION_FIELD")
        correction_id = _require(item, "correction_id")
        if correction_id in seen:
            raise Unprocessable(f"更正号在同一批里重复：{correction_id}",
                                code="E_CORRECTION_DUP")
        seen.add(correction_id)
        sub_id = _require(item, "sub_id")
        sub = subs.get(sub_id)
        if sub is None:
            raise Unprocessable(f"更正引用了未知订阅：{sub_id}",
                                code="E_CORRECTION_UNKNOWN_SUB")
        rows.append(normalize(item, sub, settings, seq=index))
    return rows
