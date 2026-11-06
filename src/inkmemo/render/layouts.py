from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import re
import sys
from datetime import date, datetime
from pathlib import Path

from lunardate import LunarDate
from playwright.async_api import Browser, Page, Playwright, async_playwright

from ..models import Battery, DueKind, LayoutResult, Task, Weather


RED = "#cf1b1b"
# Bumped whenever the approved layouts change, so a renderer upgrade forces one
# refresh instead of leaving the old frame on the panel until the content edits.
# 字体/版式变更必须进版本号，否则内容未变时 display_hash 不变，后台不会重发。
LAYOUT_VERSION = "compact-v0.19-standard-6-12"
WEEK_SHORT = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEK_LONG = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

MEASURE_JS = r"""
() => {
  const screen = document.querySelector('.screen');
  const footer = document.querySelector('.footer');
  const rect = (node) => node.getBoundingClientRect();
  const s = rect(screen);
  const f = footer ? rect(footer) : {top: s.bottom};
  const items = [...document.querySelectorAll('.row, .note')];
  const clipped = items.filter((node) => {
    const r = rect(node);
    return r.bottom > f.top - 0.5 || r.right > s.right + 0.5 || r.left < s.left - 0.5 || r.top < s.top - 0.5;
  }).length;
  const nowrap = [...document.querySelectorAll('.row .task, .row .product, .row .date, .row .owner, .row .counterpart')]
    .filter((node) => node.scrollWidth > node.clientWidth + 1).length;
  const spilled = [...document.querySelectorAll(
    '.note-details, .note-meta, .note-side, .note-project, .note-date, .note-people'
  )]
    .filter((node) => node.scrollWidth > node.clientWidth + 1).length;
  return {clipped, nowrap, spilled, screenHeight: screen.scrollHeight, screenWidth: screen.scrollWidth};
}
"""

# A wrapped task makes the row taller; the project/date and people columns
# grow with it instead of leaving a gap.
FIT_SIDES_JS = r"""
() => {
  document.querySelectorAll('.note').forEach((note) => {
    const task = note.querySelector('.note-task');
    if (!task) return;
    const style = getComputedStyle(task);
    const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2;
    const lines = Math.round(task.getBoundingClientRect().height / line);
    note.classList.toggle('tall', lines > 1);
  });
  return true;
}
"""
LUNAR_MONTHS = ("正月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "冬月", "腊月")
LUNAR_DAYS = (
    "初一",
    "初二",
    "初三",
    "初四",
    "初五",
    "初六",
    "初七",
    "初八",
    "初九",
    "初十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
    "十八",
    "十九",
    "二十",
    "廿一",
    "廿二",
    "廿三",
    "廿四",
    "廿五",
    "廿六",
    "廿七",
    "廿八",
    "廿九",
    "三十",
)


class LayoutOverflow(RuntimeError):
    pass


def _asset_root() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "design" / "approved"
    return Path(__file__).resolve().parents[3] / "design" / "approved"


def _lunar_text(day: date) -> str:
    lunar = LunarDate.from_solar_date(day.year, day.month, day.day)
    prefix = "闰" if lunar.is_leap_month else ""
    return f"农历{prefix}{LUNAR_MONTHS[lunar.month - 1]}{LUNAR_DAYS[lunar.day - 1]}"


def _battery_segments(millivolts: int | None) -> int:
    if millivolts is None:
        return 0
    return sum(millivolts >= threshold for threshold in (2900, 3000, 3100, 3200))


def battery_from_mv(millivolts: int | None) -> Battery:
    return Battery(
        millivolts=millivolts,
        segments=_battery_segments(millivolts),
        low=millivolts is not None and millivolts <= 2800,
    )


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _date_label(task: Task) -> str:
    return task.due_text or "--"


OVERDUE_ICON = (
    '<span class="due-icon" aria-label="逾期">'
    '<svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">'
    '<circle cx="9" cy="9" r="8" fill="#cf1b1b"/>'
    '<rect x="8" y="4" width="2" height="7" rx="1" fill="#fff"/>'
    '<rect x="8" y="12.5" width="2" height="2" rx="1" fill="#fff"/>'
    "</svg></span>"
)


def _tag(task: Task, *, icon: bool = False) -> str:
    if task.due_kind == DueKind.OVERDUE:
        if icon:
            return OVERDUE_ICON
        return '<span class="tag">逾</span>'
    if task.due_kind == DueKind.TODAY:
        return '<span class="tag today">今</span>'
    if task.is_near:
        return '<span class="near-dot"></span>'
    return ""


def _battery_html(battery: Battery, *, relaxed: bool = False) -> str:
    segments = "".join(
        f'<i class="seg{" on" if index < battery.segments else ""}"></i>' for index in range(4)
    )
    css = "battery low" if battery.low else "battery"
    label = battery.display
    return (
        f'<span class="{css}">'
        f'<span class="battery-icon" aria-label="看板电压">{segments}</span>'
        f"<span>{_e(label)}</span></span>"
    )


def _weather_html(
    weather: Weather,
    battery: Battery | None,
    *,
    relaxed: bool = False,
) -> str:
    city = _e(weather.city)
    if weather.available:
        condition = _e(weather.condition)
        temperature = f"{weather.temperature}°"
        range_text = f"{weather.low}—{weather.high}°"
    else:
        condition = "--"
        temperature = "--"
        range_text = "--"
    battery_html = _battery_html(battery, relaxed=relaxed) if battery is not None else ""
    return (
        f'<div class="weather"><svg class="weather-icon" viewBox="0 0 40 40" aria-hidden="true">'
        f'<use href="#wx-{_e(weather.icon)}"></use></svg>'
        f'<div class="weather-main"><span class="city">{city}</span>'
        f'<span class="condition">{condition}</span><span class="temp">{temperature}</span></div>'
        f'<div class="weather-sub"><span class="range">{range_text}</span>'
        f"{battery_html}</div></div>"
    )


def _masthead(today: date, count: int, weather: Weather, battery: Battery) -> str:
    weekday = WEEK_SHORT[today.weekday()]
    return (
        '<header class="masthead"><div class="brand"><div class="brand-kicker">INKMEMO / WPS</div>'
        f'<div class="brand-title-row"><span class="brand-title">工作备忘</span><span class="count">{count} 项</span></div>'
        '<div class="brand-sub">按截止日期排序 · 全部事项</div></div>'
        f'<div class="calendar"><div class="date-line"><span class="date-big">{today:%m.%d}</span>'
        f'<span class="weekday">{weekday}</span></div><div class="date-sub">{today.year}年 · {_lunar_text(today)}</div></div>'
        f"{_weather_html(weather, battery)}</header>"
    )


def _footer(count: int, fetched_at: datetime, status: str | None, compact: bool = False) -> str:
    stamp = fetched_at.strftime("%H:%M")
    if status:
        legend = _e(status)
        classes = "footer error"
    elif compact:
        legend = "责：责任人 · 接：对接人 · 先左栏后右栏"
        classes = "footer"
    else:
        legend = '红色：逾期 / 今天 / 3 天内 <span class="dot"></span>临近'
        classes = "footer"
    return f'<div class="{classes}"><span class="legend">{legend}</span><span class="stamp">更新 {stamp} · {count} 项</span></div>'


def _standard_rows(tasks: list[Task]) -> str:
    rows = []
    for task in tasks:
        class_name = f"row u-{task.due_kind.value}" if task.due_kind in (DueKind.OVERDUE, DueKind.TODAY) else (
            "row u-near" if task.is_near else "row"
        )
        rows.append(
            f'<div class="{class_name}"><div class="product">{_e(task.product or "--")}</div>'
            f'<div class="task">{_e(task.content)}</div><div class="date">{_tag(task, icon=True)}{_e(_date_label(task))}</div>'
            f'<div class="owner">{_e(task.owner or "--")}</div><div class="counterpart">{_e(task.counterpart or "--")}</div></div>'
        )
    return "".join(rows)


def _standard_section(
    tasks: list[Task],
    today: date,
    fetched_at: datetime,
    weather: Weather,
    battery: Battery,
    status: str | None,
) -> tuple[str, str]:
    if 11 <= len(tasks) <= 12:
        # 11–12 项沿用标准五列结构，仅收紧行高与字号以容纳两行增量；
        # 6–10 项仍使用已批准的全尺寸标准版。
        root = "screen standard-12"
    elif 13 <= len(tasks) <= 20:
        root = "screen capacity standard"
    else:
        root = "screen"
    body = (
        f'<section class="shot" id="standard"><div class="{root}">'
        f"{_masthead(today, len(tasks), weather, battery)}"
        '<div class="cols"><div>产品</div><div>事项</div><div>完成时间</div><div>责任人</div><div>对接人</div></div>'
        f'<div class="rows">{_standard_rows(tasks)}</div>'
        f"{_footer(len(tasks), fetched_at, status)}</div></section>"
    )
    return body, "standard"


def _relaxed_note(task: Task, *, columns: bool = False) -> str:
    classes = ["note"]
    if task.due_kind in (DueKind.OVERDUE, DueKind.TODAY):
        classes.append(f"u-{task.due_kind.value}")
    elif task.is_near:
        classes.append("u-near")
    date_tag = _tag(task, icon=columns)
    if columns:
        return (
            f'<article class="{" ".join(classes)}"><div class="memo-row">'
            f'<div class="memo-copy"><div class="memo-heading">'
            f'<span class="note-product">{_e(task.product or "--")}</span></div>'
            f'<div class="note-task">{_e(task.content)}</div></div>'
            f'<div class="note-side"><div class="note-date">{date_tag}{_e(_date_label(task))}</div>'
            f'<div class="memo-person"><b class="person-label">责任人</b>'
            f'<span class="person-value">{_e(task.owner or "--")}</span></div>'
            f'<div class="memo-person"><b class="person-label">对接人</b>'
            f'<span class="person-value">{_e(task.counterpart or "--")}</span></div></div></div></article>'
        )
    return (
        f'<article class="{" ".join(classes)}"><div class="memo-heading">'
        f'<span class="note-product">{_e(task.product or "--")}</span>'
        f'<span class="note-date">{date_tag}{_e(_date_label(task))}</span></div>'
        f'<div class="note-task">{_e(task.content)}</div>'
        f'<div class="note-meta"><span class="memo-person">责任人 {_e(task.owner or "--")}</span>'
        f'<span class="memo-person">对接人 {_e(task.counterpart or "--")}</span></div></article>'
    )


def _calendar_json(today: date) -> str:
    month = calendar.Calendar(firstweekday=0).monthdayscalendar(today.year, today.month)
    cells: list[str] = []
    for weekday in ("一", "二", "三", "四", "五", "六", "日"):
        weekend = " weekend" if weekday in ("六", "日") else ""
        cells.append(f'<span class="week-label{weekend}">{weekday}</span>')
    for week in month:
        for day in week:
            if day == 0:
                cells.append("<span></span>")
            elif day == today.day:
                cells.append(f'<span class="current-day">{day}</span>')
            else:
                weekday_index = (date(today.year, today.month, day).weekday())
                weekend = " weekend" if weekday_index >= 5 else ""
                cells.append(f'<span{(" class=" + chr(34) + weekend.strip() + chr(34)) if weekend else ""}>{day}</span>')
    return "".join(cells)


def _relaxed_section(
    tasks: list[Task],
    today: date,
    fetched_at: datetime,
    weather: Weather,
    battery: Battery,
    status: str | None,
    density: str | None = None,
    variant: str = "legacy",
) -> tuple[str, str]:
    size_class = density or ("few" if len(tasks) <= 6 else "medium")
    variant_class = " columns" if variant == "columns" else ""
    body = (
        '<section class="shot" id="relaxed"><div class="screen capacity relaxed relaxed-sidebar '
        f'{size_class} relaxed-detail{variant_class}">{_masthead_relaxed(today, len(tasks))}'
        f'<div class="relaxed-main">{_relaxed_header(len(tasks))}'
        f'<div class="note-columns"><div class="note-column">{"".join(_relaxed_note(task, columns=variant == "columns") for task in tasks)}</div></div></div>'
        f'{_relaxed_day_panel(today, weather, battery)}{_footer(len(tasks), fetched_at, status)}</div></section>'
    )
    return body, "relaxed"


def _masthead_relaxed(today: date, count: int) -> str:
    return ""


def _relaxed_header(count: int) -> str:
    return (
        '<header class="masthead"><div class="brand"><div class="brand-kicker">INKMEMO / WPS</div>'
        f'<div class="brand-title-row"><span class="brand-title">工作备忘</span><span class="count">{count} 项</span></div>'
        '<div class="brand-sub">按截止日期排序 · 全部事项</div></div></header>'
    )


def _relaxed_day_panel(today: date, weather: Weather, battery: Battery) -> str:
    return (
        '<aside class="day-panel" aria-label="日历与天气"><div class="day-top">'
        f'<div class="day-month">{today.year}年{today.month}月</div>'
        f'<div class="day-battery">{_battery_html(battery, relaxed=True)}</div></div>'
        f'<div class="day-head"><span class="day-number">{today.day:02d}</span>'
        f'<span class="day-week">{WEEK_LONG[today.weekday()]}</span></div>'
        f'<div class="day-lunar">{_lunar_text(today)}</div><div class="month-grid">{_calendar_json(today)}</div>'
        f"{_weather_html(weather, None, relaxed=True)}</aside>"
    )


def _compact_note(index: int, task: Task) -> str:
    classes = ["note"]
    if task.due_kind in (DueKind.OVERDUE, DueKind.TODAY):
        classes.append(f"u-{task.due_kind.value}")
    elif task.is_near:
        classes.append("u-near")
    flag = ""
    if task.due_kind == DueKind.OVERDUE:
        flag = f'<span class="note-flag">{OVERDUE_ICON}</span>'
    elif task.due_kind == DueKind.TODAY:
        flag = '<span class="note-flag today">今</span>'
    return (
        f'<article class="{" ".join(classes)}"><div class="note-when">'
        f'<div class="note-project"><span class="note-index">{index:02d}</span>'
        f'<span class="note-project-name">{_e(task.product or "--")}</span></div>'
        f'<div class="note-date">{flag}<span>{_e(_date_label(task))}</span></div></div>'
        f'<div class="note-task">{_e(task.content)}</div>'
        f'<div class="note-people"><div><b>责</b>{_e(task.owner or "--")}</div>'
        f'<div><b>接</b>{_e(task.counterpart or "--")}</div></div></article>'
    )


def _compact_section(
    tasks: list[Task],
    today: date,
    fetched_at: datetime,
    weather: Weather,
    battery: Battery,
    status: str | None,
    tier: str = "readable",
) -> tuple[str, str]:
    root = f"screen capacity standard notes {tier} compact-side count-{len(tasks)}"
    split = (len(tasks) + 1) // 2
    left = "".join(_compact_note(index + 1, task) for index, task in enumerate(tasks[:split]))
    right = "".join(
        _compact_note(split + index + 1, task) for index, task in enumerate(tasks[split:])
    )
    body = (
        f'<section class="shot" id="compact"><div class="{root}">'
        f"{_masthead(today, len(tasks), weather, battery)}"
        f'<div class="note-columns"><div class="note-column">{left}</div><div class="note-column">{right}</div></div>'
        "</div></section>"
    )
    return body, "compact"


def _empty_section(
    today: date,
    fetched_at: datetime,
    weather: Weather,
    battery: Battery,
    status: str | None,
    *,
    error: bool = False,
) -> tuple[str, str]:
    title = "云端读取失败" if error else "暂无待办"
    subtitle = (
        "暂无可用缓存 · 请检查 WPS 链接或重新登录"
        if error
        else f"成功读取于 {fetched_at:%H:%M} · 云端事项为 0"
    )
    body = (
        '<section class="shot" id="empty"><div class="screen">'
        f"{_masthead(today, 0, weather, battery)}"
        '<div class="empty-wrap"><div><div class="empty-mark"></div>'
        f'<div class="empty-title">{title}</div>'
        f'<div class="empty-sub">{subtitle}</div></div></div>'
        f"{_footer(0, fetched_at, status)}</div></section>"
    )
    return body, ("error" if error else "empty")


WEATHER_SYMBOLS = r"""
<svg class="icon-sprite" aria-hidden="true">
<symbol id="wx-partly" viewBox="0 0 40 40">
  <circle cx="14" cy="14" r="7" fill="#cf1b1b" stroke="#111" stroke-width="1.8"/>
  <g stroke="#111" stroke-width="2.2" stroke-linecap="round"><path d="M14 2v4M14 22v4M2 14h4M22 14h4M5.5 5.5l2.8 2.8M19.7 19.7l2.8 2.8"/></g>
  <path d="M13 32h16a6 6 0 0 0 .2-12A8 8 0 0 0 14 23a5 5 0 0 0-1 9z" fill="#fff" stroke="#111" stroke-width="2"/>
</symbol>
<symbol id="wx-cloud" viewBox="0 0 40 40">
  <path d="M9 32h22a7 7 0 0 0 .2-14A10 10 0 0 0 11.5 21A6 6 0 0 0 9 32z" fill="#fff" stroke="#111" stroke-width="2.2"/>
</symbol>
<symbol id="wx-rain" viewBox="0 0 40 40">
  <path d="M8 25h24a6 6 0 0 0 0-12 10 10 0 0 0-19-2A7 7 0 0 0 8 25z" fill="#fff" stroke="#111" stroke-width="2"/>
  <g stroke="#cf1b1b" stroke-width="2.4" stroke-linecap="round"><path d="M13 29l-1 5M21 29l-1 5M29 29l-1 5"/></g>
</symbol>
<symbol id="wx-snow" viewBox="0 0 40 40">
  <path d="M8 24h24a6 6 0 0 0 0-12 10 10 0 0 0-19-2A7 7 0 0 0 8 24z" fill="#fff" stroke="#111" stroke-width="2"/>
  <g fill="#cf1b1b"><circle cx="13" cy="31" r="2"/><circle cx="21" cy="33" r="2"/><circle cx="29" cy="30" r="2"/></g>
</symbol>
<symbol id="wx-thunder" viewBox="0 0 40 40">
  <path d="M8 24h24a6 6 0 0 0 0-12 10 10 0 0 0-19-2A7 7 0 0 0 8 24z" fill="#fff" stroke="#111" stroke-width="2"/>
  <path d="M22 24l-6 9h5l-2 6 8-10h-5z" fill="#cf1b1b" stroke="#111" stroke-width="1.3"/>
</symbol>
<symbol id="wx-fog" viewBox="0 0 40 40">
  <g stroke="#111" stroke-width="2.4" stroke-linecap="round"><path d="M7 13h26M4 20h32M8 27h24M12 34h16"/></g>
</symbol>
</svg>
"""


# Font ladder for the compact layout, largest first: task size, project size,
# date size, people size, project/date column width, people column width, row min-height.
COMPACT_TIERS = (
    ("lg", 18, 14, 15, 13, 72, 84, 0),
    ("standard", 17, 14, 14, 13, 68, 84, 0),
    ("readable", 16, 13, 14, 13, 68, 84, 0),
    ("tight", 15, 13, 13, 13, 66, 84, 0),
    ("max-notes", 15, 13, 12, 13, 63, 84, 0),
    ("dense", 14, 12, 12, 13, 63, 84, 0),
    ("tiny", 14, 12, 11, 13, 64, 74, 0),
)


def _compact_style() -> str:
    rules = [
        ".notes.compact-side .masthead { height: 40px; }",
        ".notes.compact-side .brand-kicker { display: none; }",
        ".notes.compact-side .brand-title-row { height: 26px; }",
        ".notes.compact-side .brand-title { font-size: 24px; line-height: 26px; }",
        ".notes.compact-side .brand-sub { font-size: 10px; line-height: 12px; }",
        ".notes.compact-side .calendar, .notes.compact-side .weather { height: 32px; padding-left: 14px; }",
        ".notes.compact-side .date-line { height: 26px; }",
        ".notes.compact-side .date-big { font-size: 26px; line-height: 26px; }",
        ".notes.compact-side .date-sub { font-size: 10px; line-height: 12px; }",
        ".notes.compact-side .weather-icon { width: 26px; height: 26px; }",
        ".notes.compact-side .weather { grid-template-columns: 26px auto; grid-template-rows: 18px 12px; }",
        ".notes.compact-side .weather-main { height: 18px; }",
        ".notes.compact-side .city, .notes.compact-side .condition { font-size: 12px; }",
        ".notes.compact-side .temp { font-size: 18px; line-height: 18px; }",
        ".notes.compact-side .weather-sub { font-size: 11px; line-height: 12px; }",
        ".notes.compact-side .note { min-height: 0; padding: 0 6px 0 11px; }",
        ".notes.compact-side .note.u-overdue::before, .notes.compact-side .note.u-today::before {"
        " left: -6px; width: 4px; top: 2px; bottom: 2px; }",
        ".notes.compact-side .note-columns { margin-top: 2px; }",
        ".notes.compact-side .note-task { font-weight: 700; }",
        ".notes.compact-side .note { align-items: center; column-gap: 7px; }",
        ".notes.compact-side .note-when { min-width: 0; }",
        ".notes.compact-side .note.tall { --side-scale: 1.12; }",
        ".notes.compact-side .note-project {"
        " font-size: calc(var(--proj) * var(--side-scale, 1)); font-weight: 800;"
        " line-height: 1.15; white-space: nowrap; overflow: hidden; }",
        ".notes.compact-side .note-index {"
        " font-size: calc(var(--proj) * var(--side-scale, 1) * 0.8); line-height: inherit;"
        " font-weight: 700; margin-right: 4px; }",
        ".notes.compact-side .note-date {"
        " font-size: calc(var(--date) * var(--side-scale, 1) * var(--date-scale, 1));"
        " display: flex; align-items: center; gap: 4px;"
        " font-weight: 700; line-height: 1.2; white-space: nowrap; }",
        ".notes.compact-side .note.u-overdue .note-date,"
        " .notes.compact-side .note.u-today .note-date { --date-scale: 1.18; }",
        ".notes.compact-side .note-flag, .notes.compact-side .due-icon {"
        " display: flex; align-items: center; flex-shrink: 0; line-height: 0; }",
        ".notes.compact-side .note-flag svg { width: calc(var(--date) * 1.05);"
        " height: calc(var(--date) * 1.05); }",
        ".notes.compact-side .note-flag.today { background: #cf1b1b; color: #fff;"
        " font-weight: 800; padding: 0 3px; vertical-align: 0; }",
        ".notes.compact-side .note-people {"
        " font-size: calc(var(--peo) * var(--side-scale, 1)); font-weight: 700;"
        " line-height: 1.15; white-space: nowrap; }",
        ".notes.compact-side .note-people b { font-weight: 400; margin-right: 5px; }",
        # 25+ items keep a slimmer masthead; the last two tiers drop it and give
        # the space back to the notes.
        ".notes.compact-side.max-notes .masthead { height: 30px; }",
        ".notes.compact-side.max-notes .brand-title { font-size: 20px; line-height: 30px; }",
        ".notes.compact-side.max-notes .brand-sub { display: none; }",
        ".notes.compact-side.max-notes .date-big { font-size: 21px; line-height: 30px; }",
        ".notes.compact-side.max-notes .date-sub { display: none; }",
        ".notes.compact-side.max-notes .note-columns { margin-top: 0; }",
        ".notes.compact-side.dense .masthead, .notes.compact-side.tiny .masthead { display: none; }",
        ".notes.compact-side.dense .note-columns,"
        " .notes.compact-side.tiny .note-columns { margin-top: 0; }",
        # Last-resort tier: fixed side type keeps 30 rows inside 480px.
        ".notes.compact-side.tiny .note.tall { --side-scale: 1; }",
    ]
    for name, task_px, project_px, date_px, people_px, when_w, people_w, row_h in COMPACT_TIERS:
        line_px = task_px + (1 if task_px <= 16 else 2)
        rules.append(
            f".notes.compact-side.{name} {{ --proj: {project_px}px; --date: {date_px}px;"
            f" --peo: {people_px}px; --whenw: {when_w}px; --peow: {people_w}px; }}"
        )
        rules.append(
            f".notes.compact-side.{name} .note {{ min-height: {row_h}px;"
            f" grid-template-columns: {when_w}px minmax(0, 1fr) {people_w}px; }}"
        )
        rules.append(
            f".notes.compact-side.{name} .note-task"
            f" {{ font-size: {task_px}px; line-height: {line_px}px; }}"
        )
    return "<style>\n" + "\n".join(rules) + "\n</style>"


COMPACT_STYLE = _compact_style()

COMPACT_PIXEL_STYLE = """
<style>
/* 紧凑版候选：模板原本使用外部字体，14px 下产生灰阶；
   统一改为 Windows 宋体 400，命中 12–16px 内嵌点阵。 */
.notes.compact-side,
.notes.compact-side * {
  font-family: "SimSun", "宋体", serif !important;
  font-weight: 400 !important;
  letter-spacing: 0 !important;
}
.notes.compact-side .footer {
  display: none !important;
}
.notes.compact-side .masthead {
  display: grid !important;
  height: 44px !important;
  margin: 0 24px !important;
  grid-template-columns: 220px 230px minmax(0, 1fr) !important;
  border-bottom: 2px solid #111 !important;
}
.notes.compact-side .brand-title-row {
  height: 26px !important;
}
.notes.compact-side .brand-title {
  font-size: 24px !important;
  line-height: 26px !important;
}
.notes.compact-side .count {
  font-size: 9px !important;
  line-height: 15px !important;
  padding: 0 5px !important;
}
.notes.compact-side .brand-kicker {
  display: none !important;
}
.notes.compact-side .brand-sub {
  display: none !important;
}
.notes.compact-side .calendar,
.notes.compact-side .weather {
  height: 34px !important;
  padding-left: 12px !important;
}
.notes.compact-side .date-line {
  height: 24px !important;
  gap: 6px !important;
}
.notes.compact-side .date-big {
  font-size: 25px !important;
  line-height: 25px !important;
}
.notes.compact-side .weekday {
  font-size: 9px !important;
  line-height: 11px !important;
}
.notes.compact-side .date-sub {
  font-size: 9px !important;
  line-height: 10px !important;
}
.notes.compact-side .weather {
  grid-template-columns: 28px auto !important;
  grid-template-rows: 20px 12px !important;
  column-gap: 7px !important;
}
.notes.compact-side .weather-icon {
  width: 28px !important;
  height: 28px !important;
}
.notes.compact-side .weather-main {
  height: 20px !important;
  gap: 4px !important;
}
.notes.compact-side .city,
.notes.compact-side .condition {
  font-size: 9px !important;
}
.notes.compact-side .temp {
  font-size: 19px !important;
  line-height: 20px !important;
}
.notes.compact-side .weather-sub {
  font-size: 9px !important;
  line-height: 10px !important;
  gap: 7px !important;
}
.notes.compact-side .note-columns {
  margin: 0 24px;
  gap: 18px;
}
.notes.compact-side .note {
  display: grid;
  grid-template-columns: 84px minmax(0, 1fr) 80px !important;
  column-gap: 6px !important;
  align-items: center;
  min-height: 0 !important;
  padding: 0 4px 0 8px !important;
}
.notes.compact-side .note-when {
  justify-self: start !important;
}
.notes.compact-side .note-task {
  font-size: 16px !important;
  line-height: 18px !important;
  text-align: left !important;
  align-self: center !important;
}
.notes.compact-side .note-project,
.notes.compact-side .note-date {
  font-size: 15px !important;
  line-height: 17px !important;
}
.notes.compact-side .note-index {
  font-size: 14px !important;
}
.notes.compact-side .note-people {
  font-size: 13px !important;
  line-height: 15px !important;
  text-align: right !important;
  justify-self: stretch !important;
  padding-right: 4px !important;
}
.notes.compact-side .note-people b {
  font-weight: 400 !important;
  margin-right: 4px;
}
.notes.compact-side .note-flag svg {
  width: 15px !important;
  height: 15px !important;
}
.notes.compact-side .note-date {
  gap: 4px;
}
.notes.compact-side.count-21 .note { min-height: 39px !important; }
.notes.compact-side.count-22 .note { min-height: 38px !important; }
.notes.compact-side.count-23 .note { min-height: 37px !important; }
.notes.compact-side.count-24 .note { min-height: 36px !important; }
.notes.compact-side.count-25 .note { min-height: 34px !important; }
.notes.compact-side.count-26 .note { min-height: 33px !important; }
.notes.compact-side.count-27 .note { min-height: 32px !important; }
.notes.compact-side.count-28 .note { min-height: 31px !important; }
.notes.compact-side.count-29 .note { min-height: 30px !important; }
.notes.compact-side.count-30 .note { min-height: 30px !important; }
.notes.compact-side.count-25 .note-task,
.notes.compact-side.count-26 .note-task,
.notes.compact-side.count-27 .note-task {
  font-size: 15px !important;
  line-height: 16px !important;
}
.notes.compact-side.count-25 .note-project,
.notes.compact-side.count-25 .note-date,
.notes.compact-side.count-26 .note-project,
.notes.compact-side.count-26 .note-date,
.notes.compact-side.count-27 .note-project,
.notes.compact-side.count-27 .note-date {
  font-size: 14px !important;
  line-height: 16px !important;
}
.notes.compact-side.count-24 .note-task {
  line-height: 17px !important;
}
.notes.compact-side.count-23 .note {
  min-height: 36px !important;
}
.notes.compact-side.count-23 .note-task {
  line-height: 17px !important;
}
.notes.compact-side.count-25 .note {
  min-height: 33px !important;
}
.notes.compact-side.count-25 .note-project,
.notes.compact-side.count-25 .note-date {
  font-size: 13px !important;
  line-height: 15px !important;
}
.notes.compact-side.count-25 .note-people {
  line-height: 14px !important;
}
.notes.compact-side.count-27 .note {
  min-height: 31px !important;
}
.notes.compact-side.count-27 .note-project,
.notes.compact-side.count-27 .note-date {
  font-size: 13px !important;
  line-height: 14px !important;
}
.notes.compact-side.count-27 .note-people {
  line-height: 12px !important;
}
.notes.compact-side.count-27 .note-task {
  line-height: 15px !important;
}
.notes.compact-side.count-29 .note {
  min-height: 29px !important;
}
.notes.compact-side.count-29 .note-task {
  font-size: 13px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-29 .note-people {
  line-height: 12px !important;
}
.notes.compact-side.count-29 .note-project,
.notes.compact-side.count-29 .note-date {
  font-size: 12px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-29 .note-flag svg {
  width: 12px !important;
  height: 12px !important;
}
.notes.compact-side.count-28 .note-task,
.notes.compact-side.count-29 .note-task,
.notes.compact-side.count-30 .note-task {
  font-size: 14px !important;
  line-height: 15px !important;
}
.notes.compact-side.count-28 .note-project,
.notes.compact-side.count-28 .note-date,
.notes.compact-side.count-29 .note-project,
.notes.compact-side.count-29 .note-date,
.notes.compact-side.count-30 .note-project,
.notes.compact-side.count-30 .note-date {
  font-size: 13px !important;
  line-height: 15px !important;
}
.notes.compact-side.count-30 .note {
  min-height: 29px !important;
}
.notes.compact-side.count-30 .note-task {
  font-size: 13px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-30 .note-people {
  line-height: 12px !important;
}
.notes.compact-side.count-30 .note-project,
.notes.compact-side.count-30 .note-date {
  font-size: 12px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-30 .note-flag svg {
  width: 12px !important;
  height: 12px !important;
}
.notes.compact-side.count-25 .note {
  min-height: 33px !important;
}
.notes.compact-side.count-25 .note-project,
.notes.compact-side.count-25 .note-date {
  font-size: 13px !important;
  line-height: 15px !important;
}
.notes.compact-side.count-25 .note-people {
  line-height: 14px !important;
}
.notes.compact-side.count-27 .note {
  min-height: 31px !important;
}
.notes.compact-side.count-27 .note-project,
.notes.compact-side.count-27 .note-date {
  font-size: 13px !important;
  line-height: 14px !important;
}
.notes.compact-side.count-27 .note-people {
  line-height: 12px !important;
}
.notes.compact-side.count-29 .note {
  min-height: 29px !important;
}
.notes.compact-side.count-29 .note-task {
  font-size: 13px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-29 .note-people {
  line-height: 12px !important;
}
.notes.compact-side.count-29 .note-project,
.notes.compact-side.count-29 .note-date {
  font-size: 12px !important;
  line-height: 13px !important;
}
.notes.compact-side.count-29 .note-flag svg {
  width: 12px !important;
  height: 12px !important;
}
</style>
"""

RELAXED_MEDIUM_STYLE = """
<style>
.relaxed-detail.medium .masthead { height: 32px; }
.relaxed-detail.medium .note { min-height: 56px; padding: 0 6px 1px 9px; }
.relaxed-detail.medium .memo-heading {
  height: 18px;
  font-size: 14px;
  line-height: 18px;
}
.relaxed-detail.medium .memo-heading .note-date {
  font-size: 18px;
  line-height: 18px;
}
.relaxed-detail.medium .note-task {
  font-size: 22px;
  line-height: 24px;
}
.relaxed-detail.medium .note-meta {
  font-size: 13px;
  line-height: 14px;
}
</style>
"""

RELAXED_COLUMNS_STYLE = """
<style>
.relaxed-detail.columns .note {
  display: flex;
  align-items: stretch;
  position: relative;
  min-height: 68px;
  padding: 0 6px 1px 9px;
}
.relaxed-detail.columns .memo-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 126px;
  column-gap: 12px;
  align-items: stretch;
  flex: 1;
  min-height: 0;
}
.relaxed-detail.columns .memo-copy {
  display: grid;
  grid-template-rows: repeat(3, minmax(0, 1fr));
  min-width: 0;
  min-height: 0;
}
.relaxed-detail.columns .memo-heading {
  display: block;
  grid-row: 1;
  align-self: center;
  height: auto;
  font-size: 17px;
  line-height: 20px;
}
.relaxed-detail.columns .memo-heading .note-product {
  display: block;
  font-size: 22px;
  line-height: 22px;
  font-weight: 800;
}
.relaxed-detail.columns .memo-heading .note-date {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  margin: 0;
  font-size: 20px;
  line-height: 22px;
  font-weight: 800;
  white-space: nowrap;
}
.relaxed-detail.columns .memo-heading .due-icon,
.relaxed-detail.columns .memo-heading .due-icon svg {
  display: block;
  width: 22px;
  height: 22px;
}
.relaxed-detail.columns .note-side {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 126px;
  display: grid;
  grid-template-rows: repeat(3, minmax(0, 1fr));
  align-content: stretch;
  align-items: center;
  min-width: 0;
  gap: 0;
}
.relaxed-detail.columns .note-side .note-date {
  display: flex;
  align-items: center;
  gap: 5px;
  grid-row: 1;
  justify-self: end;
  font-size: 22px;
  line-height: 22px;
  font-weight: 800;
  white-space: nowrap;
}
.relaxed-detail.columns .note-side .note-date .due-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: none;
}
.relaxed-detail.columns .note-side .note-date .due-icon svg {
  display: block;
  width: 22px;
  height: 22px;
}
.relaxed-detail.columns .note-side .memo-person {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 4px;
  min-width: 0;
  margin: 0;
  font-size: 12px;
  line-height: 15px;
  font-weight: 700;
  white-space: nowrap;
}
.relaxed-detail.columns .person-label {
  flex: none;
  overflow: visible;
  text-align: right;
  white-space: nowrap;
}
.relaxed-detail.columns .person-value {
  min-width: 0;
  overflow: hidden;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.relaxed-detail.columns .note-task {
  grid-row: 2 / span 2;
  align-self: center;
  min-height: 0;
  font-size: 22px;
  line-height: 24px;
  font-weight: 700;
}
.relaxed-detail.columns .note-meta {
  display: none;
}
.relaxed-detail.columns .note.u-overdue::before,
.relaxed-detail.columns .note.u-today::before {
  top: 3px;
  bottom: 3px;
  width: 2px;
}
.relaxed-detail.columns .day-panel .weather {
  height: 100px;
  grid-template-columns: 64px auto;
  grid-template-rows: 64px 24px;
  column-gap: 12px;
  margin-top: 10px;
}
.relaxed-detail.columns .day-panel .weather-main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-rows: 22px 42px;
  align-items: center;
  height: 64px;
  gap: 0 8px;
  flex-wrap: nowrap;
}
.relaxed-detail.columns .day-panel .weather-icon {
  width: 64px;
  height: 64px;
}
.relaxed-detail.columns .day-panel .city {
  grid-column: 1;
  grid-row: 1;
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
}
.relaxed-detail.columns .day-panel .condition {
  grid-column: 2;
  grid-row: 1;
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
}
.relaxed-detail.columns .day-panel .temp {
  grid-column: 1 / span 2;
  grid-row: 2;
  justify-self: end;
  font-size: 36px;
  line-height: 42px;
  margin: 0;
}
.relaxed-detail.columns .day-panel .weather-sub {
  height: 24px;
  line-height: 24px;
  font-size: 14px;
  gap: 0;
}
.relaxed-detail.columns.medium .note {
  min-height: 68px;
  padding-top: 0;
  padding-bottom: 1px;
}
.relaxed-detail.columns.medium .memo-row {
  column-gap: 10px;
}
.relaxed-detail.columns.medium .memo-heading,
.relaxed-detail.columns.medium .memo-heading .note-product {
  font-size: 20px;
  line-height: 20px;
}
.relaxed-detail.columns.medium .note-side .note-date {
  font-size: 20px;
  line-height: 20px;
}
.relaxed-detail.columns.medium .memo-heading .due-icon,
.relaxed-detail.columns.medium .memo-heading .due-icon svg {
  width: 20px;
  height: 20px;
}
.relaxed-detail.columns.medium .note-side .note-date .due-icon,
.relaxed-detail.columns.medium .note-side .note-date .due-icon svg {
  width: 20px;
  height: 20px;
}
.relaxed-detail.columns.medium .note-task {
  font-size: 22px;
  line-height: 22px;
}
.relaxed-detail.columns.medium .note-meta {
  font-size: 14px;
  line-height: 16px;
}
.relaxed-detail.columns.medium .note-side .memo-person {
  font-size: 12px;
  line-height: 15px;
}
.relaxed-detail.columns .footer {
  position: absolute;
  left: 24px;
  right: 24px;
  bottom: 4px;
  height: 16px;
  margin: 0;
  font-size: 12px;
  line-height: 16px;
  font-weight: 700;
}
</style>
"""

# 系统宋体在 28px 且落点对齐时笔画才稳定；24px 会把细横时而吃掉，故标准版整档上调。
SONG_STANDARD_STYLE = """
<style>
/* 只用 Windows 自带字体：大字为系统宋体合成粗体（矢量），小字全部落到宋体
   12–16px 的内嵌点阵，这一区间 Chromium 直接用字体自带字模，零灰阶无锯齿。 */
body, body * { font-family: "SimSun", "宋体", serif !important; font-weight: 700 !important; }
.row { height: 34px; }
/* 27px + 右侧留白：事项最多 17 字时仍与完成时间列保持 9px 间距，不再贴到图标上。 */
.row .task { font-size: 27px; line-height: 30px; padding-right: 10px; }
.row .date { font-size: 24px; line-height: 22px; }
/* 小字必须 400，且覆盖到子节点：合成粗体会破坏点阵字模、变回灰阶抗锯齿。 */
.row .product, .row .owner, .row .counterpart, .col-head,
.brand-kicker, .brand-sub, .date-sub,
.footer, .footer *, .weather-sub, .weather-sub * {
  font-family: "SimSun", "宋体", serif !important; font-weight: 400 !important;
  /* 字间距会让字模落在半像素上，点阵被重新抗锯齿，必须归零。 */
  letter-spacing: 0 !important; }
.row .product, .row .owner, .row .counterpart, .col-head { font-size: 16px; line-height: 18px; }
.brand-kicker, .brand-sub { font-size: 12px; line-height: 12px; }
.date-sub { font-size: 12px; line-height: 13px; }
.weather-sub, .footer { font-size: 12px; line-height: 14px; }
.standard-12 .row { height: 30px !important; }
.standard-12 .row .task { font-size: 22px !important; line-height: 28px !important; }
.standard-12 .row .date { font-size: 22px !important; line-height: 24px !important; }
.standard-12 .row .product { font-size: 15px !important; line-height: 18px !important; }
.standard-12 .row .owner, .standard-12 .row .counterpart {
  font-size: 15px !important; line-height: 18px !important;
}
</style>
"""

SONG_RELAXED_STYLE = """
<style>
body, body * { font-family: "SimSun", "宋体", serif !important; font-weight: 700 !important; }
.relaxed-detail.columns .note-task { font-size: 24px; line-height: 28px; }
.relaxed-detail.columns .memo-heading .note-product { font-size: 20px; line-height: 22px; }
.relaxed-detail.columns .note-meta,
.relaxed-detail.columns .note-meta * { font-family: "SimSun", "宋体", serif !important; font-weight: 400 !important; font-size: 15px; line-height: 16px; }
.relaxed-detail.columns .brand-kicker, .relaxed-detail.columns .brand-sub,
.relaxed-detail.columns .date-sub, .relaxed-detail.columns .footer,
.relaxed-detail.columns .footer * {
  font-family: "SimSun", "宋体", serif !important; font-weight: 400 !important;
  font-size: 12px; letter-spacing: 0 !important; }
.relaxed-detail.columns .weather-sub, .relaxed-detail.columns .weather-sub * {
  font-family: "SimSun", "宋体", serif !important; font-weight: 400 !important;
  font-size: 14px; letter-spacing: 0 !important; }
</style>
"""


class HtmlRenderer:
    render_timeout: float = 90.0

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if (
            self._browser is not None
            and self._browser.is_connected()
            and self._page is not None
            and not self._page.is_closed()
        ):
            return
        if self._browser is not None:
            await self.close()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(channel="msedge", headless=True)
        self._page = await self._browser.new_page(viewport={"width": 800, "height": 480})

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
        self._page = None

    def _base(self, kind: str) -> str:
        filename = {
            "standard": ("standard-v0.11", "source.html"),
            "relaxed": ("relaxed-v0.16", "source.html"),
            "compact": ("compact-v0.13", "source24.html"),
        }[kind]
        return (_asset_root() / filename[0] / filename[1]).read_text(encoding="utf-8")

    def _html(self, kind: str, section: str) -> str:
        base = self._base(kind)
        base = re.sub(r"(?s)<section class=\"shot\".*?</section>", section, base, count=1)
        if kind == "compact":
            base = base.replace(
                "</head>", COMPACT_STYLE + COMPACT_PIXEL_STYLE + "</head>", 1
            )
        elif kind == "relaxed":
            base = base.replace(
                "</head>",
                RELAXED_MEDIUM_STYLE + RELAXED_COLUMNS_STYLE + SONG_RELAXED_STYLE + "</head>",
                1,
            )
        else:
            base = base.replace("</head>", SONG_STANDARD_STYLE + "</head>", 1)
        return base.replace('<section class="shot"', WEATHER_SYMBOLS + '<section class="shot"', 1)

    async def _paint(self, document: str) -> dict:
        assert self._page is not None
        await self._page.set_content(document, wait_until="load")
        await self._page.evaluate("() => document.fonts.ready")
        await self._page.evaluate(FIT_SIDES_JS)
        return await self._page.evaluate(MEASURE_JS)

    async def render(
        self,
        tasks: list[Task],
        today: date,
        fetched_at: datetime,
        weather: Weather,
        battery: Battery,
        *,
        status: str | None = None,
        force_layout: str | None = None,
    ) -> LayoutResult:
        if len(tasks) > 30:
            raise LayoutOverflow("LAYOUT_OVERFLOW：事项超过 30 项")
        await self.start()
        assert self._page is not None
        attempts = [force_layout] if force_layout else self._attempts(len(tasks))
        errors: list[str] = []
        async with self._lock:
            for layout in attempts:
                if layout == "empty":
                    section, actual = _empty_section(today, fetched_at, weather, battery, status)
                    base_kind = "standard"
                elif layout == "error":
                    section, actual = _empty_section(
                        today, fetched_at, weather, battery, status, error=True
                    )
                    base_kind = "standard"
                elif layout == "standard":
                    section, actual = _standard_section(tasks, today, fetched_at, weather, battery, status)
                    base_kind = "standard"
                elif layout.startswith("relaxed"):
                    density = layout.split(":", 1)[1] if ":" in layout else None
                    variant = "legacy" if layout.startswith("relaxed-legacy") else "columns"
                    section, actual = _relaxed_section(
                        tasks, today, fetched_at, weather, battery, status, density, variant
                    )
                    base_kind = "relaxed"
                elif layout.startswith("compact"):
                    tier = layout.split(":", 1)[1] if ":" in layout else "readable"
                    section, actual = _compact_section(
                        tasks, today, fetched_at, weather, battery, status, tier
                    )
                    base_kind = "compact"
                else:
                    raise ValueError(f"unknown layout: {layout}")
                document = self._html(base_kind, section)
                try:
                    measurement = await asyncio.wait_for(
                        self._paint(document), self.render_timeout
                    )
                except asyncio.TimeoutError:
                    # A hung Edge would otherwise block the background loop
                    # forever with no further checks or notifications.
                    await self.close()
                    raise

                if (
                    measurement["clipped"] == 0
                    and measurement["nowrap"] == 0
                    and measurement["spilled"] == 0
                    and measurement["screenHeight"] <= 480
                    and measurement["screenWidth"] <= 800
                ):
                    png = await self._page.screenshot(
                        type="png",
                        clip={"x": 0, "y": 0, "width": 800, "height": 480},
                    )
                    black, red = png_to_planes(png)
                    signature = _signature(tasks, actual, today, status)
                    return LayoutResult(layout=actual, png=png, black=black, red=red, display_hash=signature)
                errors.append(f"{layout}: {measurement}")
        raise LayoutOverflow("LAYOUT_OVERFLOW：" + "; ".join(errors))

    @staticmethod
    def _attempts(count: int) -> list[str]:
        if count == 0:
            return ["empty"]
        # Largest first: the first layout whose text still fits wins, so long
        # text steps down a size instead of being truncated.
        tiers = [f"compact:{name}" for name, *_ in COMPACT_TIERS]
        if count <= 5:
            # 宽松版最多 5 项；6 项起进入标准版，不做压缩或分页。
            # 长文本先在宽松版内降一档密度，仍放不下才换标准版。
            return ["relaxed", "relaxed:medium", "standard", *tiers]
        if count <= 12:
            # 标准版覆盖 6–12 项；11–12 项使用 standard-12 容量子样式。
            return ["standard", *tiers]
        return tiers


def _signature(
    tasks: list[Task],
    layout: str,
    today: date,
    status: str | None,
) -> str:
    payload = {
        "v": LAYOUT_VERSION,
        "layout": layout,
        "today": today.isoformat(),
        "status": status,
        "tasks": [
            [task.product, task.content, task.due_text, task.due_kind.value, task.owner, task.counterpart]
            for task in tasks
        ],
    }
    return hashlib.sha256(
        repr(payload).encode("utf-8")
    ).hexdigest()


def png_to_planes(png: bytes) -> tuple[bytes, bytes]:
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != (800, 480):
        raise ValueError(f"unexpected frame size: {image.size}")
    black = bytearray(48_000)
    red = bytearray(48_000)
    pixels = image.load()
    for y in range(480):
        row_offset = y * 100
        for x in range(800):
            r, g, b = pixels[x, y]
            is_red = r > 110 and r - max(g, b) > 20
            gray = round(0.299 * r + 0.587 * g + 0.114 * b)
            black_bit = 1 if is_red or gray >= 175 else 0
            red_bit = 0 if is_red else 1
            bit = 7 - (x % 8)
            black[row_offset + x // 8] |= black_bit << bit
            red[row_offset + x // 8] |= red_bit << bit
    return bytes(black), bytes(red)


def overlay_failure_footer(png: bytes, message: str, fetched_at: datetime) -> LayoutResult:
    import io

    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != (800, 480):
        raise ValueError(f"unexpected frame size: {image.size}")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 446, 800, 480), fill="white")
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 11)
    draw.text((24, 454), message, fill=RED, font=font)
    stamp = f"缓存更新 {fetched_at:%H:%M}"
    width = draw.textlength(stamp, font=font)
    draw.text((776 - width, 454), stamp, fill="#333333", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    rendered = output.getvalue()
    black, red = png_to_planes(rendered)
    return LayoutResult(
        layout="cached-error",
        png=rendered,
        black=black,
        red=red,
        display_hash=hashlib.sha256(rendered).hexdigest(),
    )
