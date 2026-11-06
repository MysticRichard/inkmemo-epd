from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from inkmemo.domain import SourceFormatError, parse_due, parse_grid, sort_tasks
from inkmemo.models import Grid, MergeRange
from inkmemo.sources.xlsx import read_xlsx


def make_grid(values, merges=()):
    return Grid(
        values=values,
        merges=list(merges),
        document_title="fixture",
        sheet_name="工作表1",
        fetched_at=datetime.now(),
    )


def test_header_mapping_merge_propagation_and_missing_fields():
    values = [
        ["责任人", "对接人", "完成时间", "事项", "产品", "", ""],
        ["甲", "乙", "11.01", "任务一", "项目甲", "", ""],
        ["丙", "丁", "11.02", "任务二", "", "", ""],
        [None, None, None, None, None, None, None],
        ["", "", "11月", "任务三", "项目乙", "", ""],
    ]
    grid = make_grid(values, [MergeRange(1, 2, 4, 4)])
    tasks = sort_tasks(parse_grid(grid, 2026, date(2026, 11, 2)))
    assert [task.content for task in tasks] == ["任务一", "任务二", "任务三"]
    assert tasks[0].product == "项目甲"
    assert tasks[1].product == "项目甲"
    assert tasks[2].product == "项目乙"
    assert tasks[0].due_kind == "overdue"
    assert tasks[1].due_kind == "today"
    assert tasks[2].due_kind == "undated"
    assert tasks[2].missing_fields == ["责任人", "对接人"]


def test_dates_do_not_guess_without_year():
    assert parse_due("11.03", None) == (None, "11.03")
    assert parse_due("11.03", 2026)[0] == date(2026, 11, 3)
    assert parse_due("12.05前", 2026)[0] == date(2026, 12, 5)
    assert parse_due("11月", 2026) == (None, "11月")
    assert parse_due("4月前", 2026) == (None, "4月前")
    assert parse_due("45000", 2026)[0] == date(2023, 3, 15)


def test_duplicate_header_is_rejected():
    with pytest.raises(SourceFormatError):
        parse_grid(
            make_grid([["产品", "事项", "完成时间", "责任人", "对接人", "事项"]]),
            2026,
            date(2026, 11, 2),
        )


def test_xlsx_fixture(tmp_path: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "工作表1"
    sheet.append(["产品", "事项", "完成时间", "责任人", "对接人"])
    sheet.append(["项目甲", "任务一", "11.03", "甲", "乙"])
    sheet.append(["项目甲", "任务二", "11.01", "丙", "丁"])
    sheet.merge_cells("A2:A3")
    path = tmp_path / "fixture.xlsx"
    workbook.save(path)
    tasks = sort_tasks(parse_grid(read_xlsx(path), 2026, date(2026, 11, 2)))
    assert [task.content for task in tasks] == ["任务二", "任务一"]
    assert [task.product for task in tasks] == ["项目甲", "项目甲"]


def test_sort_order_is_overdue_today_then_nearest_and_keeps_source_order():
    tasks = sort_tasks(
        parse_grid(
            make_grid(
                [
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项", "待定甲", "待确认", "甲", "乙"],
                    ["项", "未来远", "11.20", "甲", "乙"],
                    ["项", "未来近", "11.05", "甲", "乙"],
                    ["项", "同日先", "11.05", "甲", "乙"],
                    ["项", "今天", "11.02", "甲", "乙"],
                    ["项", "逾期乙", "10.30", "甲", "乙"],
                    ["项", "逾期甲", "10.28", "甲", "乙"],
                    ["项", "待定乙", "待确认", "甲", "乙"],
                ]
            ),
            2026,
            date(2026, 11, 2),
        )
    )

    assert [task.content for task in tasks] == [
        "逾期甲",
        "逾期乙",
        "今天",
        "未来近",
        "同日先",
        "未来远",
        "待定甲",
        "待定乙",
    ]
