from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Iterable

from openpyxl.utils.datetime import from_excel

from .models import DueKind, Grid, Task


HEADERS = {
    "产品": "product",
    "事项": "content",
    "完成时间": "due",
    "责任人": "owner",
    "对接人": "counterpart",
}


class SourceFormatError(ValueError):
    pass


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _merge_anchor(grid: Grid, row: int, col: int) -> tuple[int, int]:
    for merge in grid.merges:
        if merge.contains(row, col):
            return merge.row_from, merge.col_from
    return row, col


def _value(grid: Grid, row: int, col: int) -> str:
    anchor_row, anchor_col = _merge_anchor(grid, row, col)
    if 0 <= anchor_row < len(grid.values) and 0 <= anchor_col < len(grid.values[anchor_row]):
        return _text(grid.values[anchor_row][anchor_col])
    return ""


def _header_map(grid: Grid) -> tuple[int, dict[str, int]]:
    for row_index, row in enumerate(grid.values[:12]):
        mapping: dict[str, int] = {}
        for col_index, value in enumerate(row):
            label = re.sub(r"\s+", "", _text(value))
            key = HEADERS.get(label)
            if key:
                if key in mapping:
                    raise SourceFormatError(f"表头重复：{label}")
                mapping[key] = col_index
        if set(mapping) == set(HEADERS.values()):
            return row_index, mapping
    raise SourceFormatError("未找到完整表头：产品、事项、完成时间、责任人、对接人")


def parse_due(value: str, board_year: int | None) -> tuple[date | None, str]:
    text = value.strip()
    if not text:
        return None, ""
    normalized = text.replace("年", "-").replace("月", "-").replace("日", "-").replace("/", "-")
    normalized = re.sub(r"-+", "-", normalized).strip("-")

    for pattern in ("%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, pattern).date(), text
        except ValueError:
            pass

    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", normalized)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))), text

    date_candidate = re.sub(r"\s*前$", "", text)
    date_candidate = re.sub(r"\s*号$", "", date_candidate)
    dotted = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", date_candidate)
    dashed = re.fullmatch(r"(\d{1,2})-(\d{1,2})", date_candidate.replace("/", "-"))
    match = dotted or dashed
    if match and board_year is not None:
        try:
            return date(board_year, int(match.group(1)), int(match.group(2))), text
        except ValueError:
            return None, text

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        serial = float(text)
        if 20_000 <= serial <= 80_000:
            try:
                parsed = from_excel(serial)
                return parsed.date() if isinstance(parsed, datetime) else parsed, text
            except (ValueError, OverflowError):
                pass
    return None, text


def _due_kind(due: date | None, today: date, near_days: int) -> tuple[DueKind, bool]:
    if due is None:
        return DueKind.UNDATED, False
    if due < today:
        return DueKind.OVERDUE, False
    if due == today:
        return DueKind.TODAY, False
    return DueKind.UPCOMING, (due - today) <= timedelta(days=near_days)


def parse_grid(grid: Grid, board_year: int | None, today: date, near_days: int = 3) -> list[Task]:
    header_row, mapping = _header_map(grid)
    tasks: list[Task] = []
    for row_index in range(header_row + 1, len(grid.values)):
        values = {key: _value(grid, row_index, col) for key, col in mapping.items()}
        if not values["content"]:
            continue
        due_date, due_text = parse_due(values["due"], board_year)
        due_kind, is_near = _due_kind(due_date, today, near_days)
        missing = [label for label, key in HEADERS.items() if not values[HEADERS[label]]]
        identity = "\x1f".join(
            [str(row_index), values["product"], values["content"], values["due"], values["owner"], values["counterpart"]]
        )
        tasks.append(
            Task(
                task_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12],
                product=values["product"],
                content=values["content"],
                due_text=due_text,
                due_date=due_date,
                due_kind=due_kind,
                owner=values["owner"],
                counterpart=values["counterpart"],
                source_row=row_index,
                missing_fields=missing,
                is_near=is_near,
            )
        )
    return tasks


def sort_tasks(tasks: Iterable[Task]) -> list[Task]:
    rank = {
        DueKind.OVERDUE: 0,
        DueKind.TODAY: 1,
        DueKind.UPCOMING: 2,
        DueKind.UNDATED: 3,
    }
    return sorted(
        tasks,
        key=lambda task: (
            rank[task.due_kind],
            task.due_date or date.max,
            task.source_row,
        ),
    )


def content_hash(tasks: Iterable[Task], board_year: int | None) -> str:
    payload = {
        "year": board_year,
        "tasks": [
            [
                task.product,
                task.content,
                task.due_text,
                task.due_date.isoformat() if task.due_date else None,
                task.due_kind.value,
                task.owner,
                task.counterpart,
                task.source_row,
            ]
            for task in tasks
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
