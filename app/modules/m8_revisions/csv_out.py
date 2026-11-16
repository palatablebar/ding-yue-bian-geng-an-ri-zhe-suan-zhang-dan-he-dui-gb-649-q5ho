"""M8 调整单 CSV：原额、新额、差额、方向与更正依据，一行就是一张受影响账单。

与账单 CSV 同一套金额文本规则（两位小数、BOM、不做本地化），
复核人可以直接拿「账单额 + 调整差额」复算出修订后金额。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

ADJUSTMENT_HEADER = [
    "修订版", "更正号", "账单号", "订阅号", "账户", "原账单额(元)", "修订后(元)",
    "差额绝对值(元)", "方向", "差额带符号(元)", "说明",
]

DIRECTION_TEXT = {"refund": "应退", "surcharge": "应补", "unchanged": "不变"}


def _writer() -> tuple[io.StringIO, "csv._writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def money(cents: int) -> str:
    return f"{Decimal(int(cents)) / 100:.2f}"


def with_bom(text: str) -> str:
    return "﻿" + text


def adjustments_csv(rows: list[dict]) -> str:
    buffer, writer = _writer()
    writer.writerow(ADJUSTMENT_HEADER)
    for row in rows:
        writer.writerow([
            f"R{row['revision_no']}", row["correction_id"], row["invoice_id"],
            row["sub_id"], row.get("account", ""), money(row["before_cents"]),
            money(row["after_cents"]), money(abs(row["delta_cents"])),
            DIRECTION_TEXT.get(row["direction"], row["direction"]),
            money(row["delta_cents"]), row.get("note", ""),
        ])
    return buffer.getvalue()
