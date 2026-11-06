import asyncio
import io
from dataclasses import replace
from datetime import date, datetime

import pytest
from PIL import Image

from inkmemo.models import DueKind, Task, Weather
from inkmemo.render.layouts import (
    COMPACT_PIXEL_STYLE,
    SONG_RELAXED_STYLE,
    SONG_STANDARD_STYLE,
    HtmlRenderer,
    LayoutOverflow,
    _battery_html,
    _lunar_text,
    _relaxed_day_panel,
    _tag,
    battery_from_mv,
    overlay_failure_footer,
    png_to_planes,
)


def test_override_styles_only_reference_windows_builtin_fonts():
    # 字体必须随 Windows 自带，换电脑不能依赖额外安装的字库。
    styles = SONG_STANDARD_STYLE + SONG_RELAXED_STYLE + COMPACT_PIXEL_STYLE
    assert 'font-family: "SimSun", "宋体", serif' in styles
    for external in ("Noto", "Source Han", "思源"):
        assert external not in styles


def tasks(count):
    return [
        Task(
            task_id=f"{index:012d}",
            product=f"项目 {index // 3 + 1}",
            content=f"虚构事项 {index + 1} 完成状态与交付条件",
            due_text=f"11.{index % 28 + 1:02d}",
            due_date=None,
            due_kind=DueKind.UNDATED,
            owner=f"甲{index % 4}",
            counterpart=f"乙{index % 3}",
            source_row=index + 1,
        )
        for index in range(count)
    ]


def test_approved_layout_selection_and_planes():
    async def run():
        renderer = HtmlRenderer()
        try:
            weather = Weather("上海", "晴", "sun", 19, 12, 19)
            relaxed = await renderer.render(
                tasks(5), date(2026, 11, 2), datetime(2026, 11, 2, 14, 30), weather, battery_from_mv(3180)
            )
            standard = await renderer.render(
                tasks(10), date(2026, 11, 2), datetime(2026, 11, 2, 14, 30), weather, battery_from_mv(3180)
            )
            compact12 = await renderer.render(
                tasks(12), date(2026, 11, 2), datetime(2026, 11, 2, 14, 30), weather, battery_from_mv(3180)
            )
            compact24 = await renderer.render(
                tasks(24), date(2026, 11, 2), datetime(2026, 11, 2, 14, 30), weather, battery_from_mv(3180)
            )
            compact = await renderer.render(
                tasks(30), date(2026, 11, 2), datetime(2026, 11, 2, 14, 30), weather, battery_from_mv(3180)
            )
            assert relaxed.layout == "relaxed"
            assert standard.layout == "standard"
            assert compact12.layout == "standard"
            assert compact24.layout == "compact"
            assert compact.layout == "compact"
            for frame in (relaxed, standard, compact12, compact24, compact):
                assert len(frame.black) == 48_000
                assert len(frame.red) == 48_000
                assert frame.png.startswith(b"\x89PNG")
            cached = overlay_failure_footer(
                standard.png,
                "云端读取失败 · 虚构测试",
                datetime(2026, 11, 2, 14, 0),
            )
            assert cached.layout == "cached-error"
            assert len(cached.black) == 48_000
            assert len(cached.red) == 48_000
        finally:
            await renderer.close()

    asyncio.run(run())


def test_tiers_follow_measured_capacity():
    assert HtmlRenderer._attempts(5)[0] == "relaxed"
    # 用户 2026-11-05 修订：标准版覆盖 6–12 项。
    assert HtmlRenderer._attempts(6)[0] == "standard"
    assert HtmlRenderer._attempts(12)[0] == "standard"
    assert HtmlRenderer._attempts(13)[0].startswith("compact")
    assert HtmlRenderer._attempts(20)[0].startswith("compact")
    assert HtmlRenderer._attempts(30)[0].startswith("compact")


def test_twelve_normal_items_still_fit_the_standard_layout():
    async def run():
        renderer = HtmlRenderer()
        try:
            result = await renderer.render(
                tasks(12),
                date(2026, 11, 5),
                datetime(2026, 11, 5, 14, 30),
                Weather.unavailable("杭州"),
                battery_from_mv(None),
            )
            assert result.layout == "standard"
            assert Image.open(io.BytesIO(result.png)).size == (800, 480)
        finally:
            await renderer.close()

    asyncio.run(run())


def test_relaxed_layout_retries_readable_density_for_wrapped_tasks():
    async def run():
        renderer = HtmlRenderer()
        try:
            long_tasks = tasks(5)
            for index in range(len(long_tasks)):
                long_tasks[index] = replace(
                    long_tasks[index],
                    content="虚构长事项需要显示完整交付条件与后续确认节点",
                )
            frame = await renderer.render(
                long_tasks,
                date(2026, 11, 3),
                datetime(2026, 11, 3, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert frame.layout == "relaxed"
            assert await renderer._page.evaluate(
                "() => document.querySelector('.screen').classList.contains('medium')"
            )
        finally:
            await renderer.close()

    asyncio.run(run())


def test_relaxed_columns_candidate_keeps_fields_and_weather_separated():
    async def run():
        renderer = HtmlRenderer()
        try:
            candidate = tasks(5)
            candidate[0] = replace(
                candidate[0],
                due_text="3.18前",
                due_kind=DueKind.OVERDUE,
                owner="虚构甲",
                counterpart="虚构乙丙",
            )
            candidate[4] = replace(
                candidate[4],
                content="虚构长事项需要显示完整交付条件与后续确认节点",
            )
            frame = await renderer.render(
                candidate,
                date(2026, 11, 3),
                datetime(2026, 11, 3, 14, 30),
                Weather("杭州市", "阴", "cloud", 20, 17, 21),
                battery_from_mv(None),
                force_layout="relaxed-columns:medium",
            )
            assert frame.layout == "relaxed"
            html = await renderer._page.content()
            assert "relaxed-detail columns" in html
            assert html.count('class="person-label">责任人') == 5
            assert html.count('class="person-label">对接人') == 5
            geometry = await renderer._page.evaluate(
                """() => {
                    const rows = [...document.querySelectorAll('.note')].map((note) => {
                        const task = note.querySelector('.note-task').getBoundingClientRect();
                        const side = note.querySelector('.note-side').getBoundingClientRect();
                            const copy = note.querySelector('.memo-copy').getBoundingClientRect();
                            const copyStyle = getComputedStyle(note.querySelector('.memo-copy'));
                            const headingStyle = getComputedStyle(note.querySelector('.memo-heading'));
                            const taskStyle = getComputedStyle(note.querySelector('.note-task'));
                        const labels = [...note.querySelectorAll('.person-label')].map((n) => n.getBoundingClientRect());
                        const values = [...note.querySelectorAll('.person-value')].map((n) => n.getBoundingClientRect());
                        const people = [...note.querySelectorAll('.memo-person')].map((n) => n.getBoundingClientRect());
                        const valueOverflow = [...note.querySelectorAll('.person-value')].some(
                            (n) => n.scrollWidth > n.clientWidth + 1
                        );
                        const sideItems = [
                            note.querySelector('.note-date'),
                            ...note.querySelectorAll('.memo-person'),
                        ].map((n) => n.getBoundingClientRect());
                        const dateNode = note.querySelector('.note-date');
                        const dateStyle = getComputedStyle(dateNode);
                        const productStyle = getComputedStyle(note.querySelector('.note-product'));
                        const personStyle = getComputedStyle(note.querySelector('.memo-person'));
                        const icon = note.querySelector('.due-icon');
                        const dateRect = dateNode.getBoundingClientRect();
                        const iconRect = icon?.getBoundingClientRect();
                        const taskFontSize = parseFloat(taskStyle.fontSize);
                        const taskFontWeight = parseInt(taskStyle.fontWeight, 10);
                        return {
                            taskRight: task.right,
                            sideLeft: side.left,
                            copyTop: copy.top,
                            copyBottom: copy.bottom,
                            sideTop: side.top,
                            sideBottom: side.bottom,
                            copyDisplay: copyStyle.display,
                            copyRows: copyStyle.gridTemplateRows,
                            headingGridRow: headingStyle.gridRow,
                            taskGridRow: taskStyle.gridRow,
                            labelRights: labels.map((r) => r.right),
                            valuesRight: values.map((r) => r.right),
                            peopleRight: people.map((r) => r.right),
                            valueOverflow,
                            sideRights: sideItems.map((r) => r.right),
                            sideCenters: sideItems.map((r) => (r.top + r.bottom) / 2),
                            dateFontSize: parseFloat(dateStyle.fontSize),
                            dateFontWeight: parseInt(dateStyle.fontWeight, 10),
                            productFontSize: parseFloat(productStyle.fontSize),
                            taskFontSize,
                            taskFontWeight,
                            personFontSize: parseFloat(personStyle.fontSize),
                            dateCenter: (dateRect.top + dateRect.bottom) / 2,
                            iconCenter: iconRect ? (iconRect.top + iconRect.bottom) / 2 : null,
                        };
                    });
                    const weather = document.querySelector('.day-panel .weather');
                    const temp = weather.querySelector('.temp').getBoundingClientRect();
                    const range = weather.querySelector('.weather-sub').getBoundingClientRect();
                    const weatherStyle = getComputedStyle(weather);
                    const cityStyle = getComputedStyle(weather.querySelector('.city'));
                    const tempStyle = getComputedStyle(weather.querySelector('.temp'));
                    const rangeStyle = getComputedStyle(weather.querySelector('.range'));
                    const footerStyle = getComputedStyle(document.querySelector('.footer'));
                    return {
                        rows,
                        weatherHeight: parseFloat(weatherStyle.height),
                        cityFontSize: parseFloat(cityStyle.fontSize),
                        tempFontSize: parseFloat(tempStyle.fontSize),
                        rangeFontSize: parseFloat(rangeStyle.fontSize),
                        tempBottom: temp.bottom,
                        rangeTop: range.top,
                        footerFontSize: parseFloat(footerStyle.fontSize),
                        footerFontWeight: parseInt(footerStyle.fontWeight, 10),
                    };
                }"""
            )
            for row in geometry["rows"]:
                assert row["taskRight"] <= row["sideLeft"]
                assert abs(row["copyTop"] - row["sideTop"]) < 1
                assert abs(row["copyBottom"] - row["sideBottom"]) < 1
                assert row["copyDisplay"] == "grid"
                assert row["headingGridRow"] == "1"
                assert row["taskGridRow"] == "2 / span 2"
                assert max(row["sideRights"]) - min(row["sideRights"]) < 1
                assert max(row["peopleRight"]) - min(row["peopleRight"]) < 1
                assert max(row["valuesRight"]) - min(row["valuesRight"]) < 1
                assert abs(max(row["valuesRight"]) - max(row["sideRights"])) < 1
                assert not row["valueOverflow"]
                first_gap = row["sideCenters"][1] - row["sideCenters"][0]
                second_gap = row["sideCenters"][2] - row["sideCenters"][1]
                assert abs(first_gap - second_gap) < 1.5
                assert row["dateFontSize"] >= 20
                # 宋体没有真正的粗体档，加粗会带回抗锯齿毛边，故允许常规字重。
                assert row["dateFontWeight"] >= 400
                assert row["productFontSize"] >= 20
                assert abs(row["productFontSize"] - row["dateFontSize"]) < 1
                assert row["taskFontSize"] > row["productFontSize"]
                # 同上：宋体只有常规字重，靠字号而非字重拉开层级。
                assert row["taskFontWeight"] >= 400
                assert row["personFontSize"] <= 12
                if row["iconCenter"] is not None:
                    assert abs(row["iconCenter"] - row["dateCenter"]) < 1
            assert geometry["tempBottom"] <= geometry["rangeTop"] + 1
            assert geometry["weatherHeight"] >= 96
            assert geometry["cityFontSize"] >= 14
            assert geometry["tempFontSize"] >= 36
            assert geometry["rangeFontSize"] >= 14
            assert geometry["footerFontSize"] >= 12
            assert geometry["footerFontWeight"] >= 400
        finally:
            await renderer.close()

    asyncio.run(run())


def test_relaxed_battery_is_not_duplicated_in_weather():
    panel = _relaxed_day_panel(
        date(2026, 11, 2),
        Weather("上海", "晴", "sun", 19, 12, 19),
        battery_from_mv(3180),
    )
    assert panel.count('class="battery"') == 1


def test_unavailable_battery_shows_explicit_label():
    assert "电量 --" in _battery_html(battery_from_mv(None))


def test_layout_falls_back_to_denser_tier_instead_of_clipping():
    from inkmemo.render.layouts import HtmlRenderer

    tiers = [
        "compact:lg",
        "compact:standard",
        "compact:readable",
        "compact:tight",
        "compact:max-notes",
        "compact:dense",
        "compact:tiny",
    ]
    assert HtmlRenderer._attempts(0) == ["empty"]
    assert HtmlRenderer._attempts(20) == tiers
    assert HtmlRenderer._attempts(21) == tiers
    assert HtmlRenderer._attempts(30) == tiers


def test_standard_overdue_uses_icon_before_date():
    task = Task(
        task_id="000000000001",
        product="虚构项目",
        content="虚构逾期事项",
        due_text="10.27",
        due_date=None,
        due_kind=DueKind.OVERDUE,
        owner="甲",
        counterpart="乙",
        source_row=1,
    )

    icon = _tag(task, icon=True)
    assert 'class="due-icon"' in icon
    assert "<svg" in icon
    assert "triangle" not in icon.lower()
    assert "<polygon" not in icon.lower()
    assert '<span class="tag">逾</span>' not in icon
    assert '<span class="tag">逾</span>' == _tag(task)


def test_red_pixels_are_not_written_to_black_plane():
    image = Image.new("RGB", (800, 480), "white")
    image.putpixel((0, 0), (207, 27, 27))
    image.putpixel((1, 0), (17, 17, 17))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    black, red = png_to_planes(buffer.getvalue())

    assert black[0] & 0b10000000
    assert not red[0] & 0b10000000
    assert not black[0] & 0b01000000
    assert red[0] & 0b01000000


def test_more_than_30_is_overflow():
    async def run():
        renderer = HtmlRenderer()
        try:
            with pytest.raises(LayoutOverflow):
                await renderer.render(
                    tasks(31),
                    date(2026, 11, 2),
                    datetime(2026, 11, 2, 14, 30),
                    Weather.unavailable("上海"),
                    battery_from_mv(None),
                )
        finally:
            await renderer.close()

    asyncio.run(run())


def test_compact_full_overdue_table_keeps_all_fields_inside_frame():
    full_table = [
        replace(
            task,
            product="试验组",
            content="核对样件尺寸并整理测试记录后提交本轮评审资料",
            due_text="10.18前",
            due_kind=DueKind.OVERDUE,
            owner="甲乙丙",
            counterpart="丁戊己庚",
        )
        for task in tasks(30)
    ]

    async def run():
        renderer = HtmlRenderer()
        try:
            frame = await renderer.render(
                full_table,
                date(2026, 11, 3),
                datetime(2026, 11, 3, 17, 0),
                Weather.unavailable("测试城市"),
                battery_from_mv(None),
            )
            assert frame.layout == "compact"
            assert await renderer._page.locator(".note").count() == 30
            assert await renderer._page.locator(".masthead").is_visible()
            assert await renderer._page.locator(".note").first.evaluate(
                """note => {
                  const icon = note.querySelector('.note-flag').getBoundingClientRect();
                  const date = note.querySelector('.note-date').getBoundingClientRect();
                  return icon.left === date.left;
                }"""
            )
            assert await renderer._page.locator(".note-task").all_text_contents() == [
                task.content for task in full_table
            ]
            assert await renderer._page.locator(".note-people").evaluate_all(
                """nodes => nodes.every(node => parseFloat(getComputedStyle(node).fontSize) >= 13)"""
            )
        finally:
            await renderer.close()

    asyncio.run(run())


def test_compact_candidate_left_aligns_content_and_right_aligns_people():
    candidate = tasks(24)

    async def run():
        renderer = HtmlRenderer()
        try:
            await renderer.render(
                candidate,
                date(2026, 11, 3),
                datetime(2026, 11, 3, 17, 0),
                Weather("测试城市", "晴", "sun", 20, 15, 22),
                battery_from_mv(None),
            )
            assert await renderer._page.locator(".compact-side .masthead").is_visible()
            assert await renderer._page.locator(".compact-side .weather").is_visible()
            assert await renderer._page.locator(".compact-side .masthead").evaluate(
                "node => node.getBoundingClientRect().height >= 40"
            )
            assert await renderer._page.locator(".compact-side .brand-kicker").is_hidden()
            assert await renderer._page.locator(".compact-side .brand-sub").is_hidden()
            assert await renderer._page.locator(".note-task").first.evaluate(
                "node => getComputedStyle(node).textAlign === 'left'"
            )
            assert await renderer._page.locator(".note-when").first.evaluate(
                "node => getComputedStyle(node).justifySelf === 'start'"
            )
            assert await renderer._page.locator(".note-people").first.evaluate(
                """node => getComputedStyle(node).textAlign === 'right'"""
            )
            assert await renderer._page.locator(".note").count() == 24
            geometry = await renderer._page.locator(".screen").evaluate(
                """node => {
                    const rect = node.getBoundingClientRect();
                    const notes = [...node.querySelectorAll('.note')];
                    return {
                        height: rect.height,
                        bottom: Math.max(...notes.map(note => note.getBoundingClientRect().bottom)),
                    };
                }"""
            )
            assert geometry["bottom"] <= 480
        finally:
            await renderer.close()

    asyncio.run(run())


def test_long_task_text_falls_back_to_compact_without_truncation():
    long_tasks = tasks(12)
    long_tasks[0] = Task(
        task_id="000000000099",
        product="项目 1",
        content="虚构超长事项" * 20,
        due_text="11.03",
        due_date=None,
        due_kind=DueKind.UNDATED,
        owner="甲0",
        counterpart="乙0",
        source_row=99,
    )

    async def run():
        renderer = HtmlRenderer()
        try:
            result = await renderer.render(
                long_tasks,
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert result.layout == "compact"
            with pytest.raises(LayoutOverflow):
                await renderer.render(
                    [replace(long_tasks[0], content="虚构超长事项" * 400)] + long_tasks[1:],
                    date(2026, 11, 2),
                    datetime(2026, 11, 2, 14, 30),
                    Weather.unavailable("上海"),
                    battery_from_mv(None),
                )
        finally:
            await renderer.close()

    asyncio.run(run())


def test_lunar_text_matches_known_dates():
    # 2026-02-17 is Chinese New Year; 2026-11-02 is the date on the approved board.
    assert _lunar_text(date(2026, 2, 17)) == "\u519c\u5386\u6b63\u6708\u521d\u4e00"
    assert _lunar_text(date(2026, 11, 2)) == "\u519c\u5386\u4e5d\u6708\u5eff\u56db"


def test_new_day_changes_display_hash():
    async def run():
        renderer = HtmlRenderer()
        try:
            first = await renderer.render(
                tasks(10),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            nextday = await renderer.render(
                tasks(10),
                date(2026, 11, 3),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert first.layout == nextday.layout == "standard"
            assert first.display_hash != nextday.display_hash
        finally:
            await renderer.close()

    asyncio.run(run())


def test_weather_change_alone_does_not_change_display_hash():
    async def run():
        renderer = HtmlRenderer()
        try:
            sunny = await renderer.render(
                tasks(12),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather("上海", "晴", "sun", 19, 12, 19),
                battery_from_mv(None),
            )
            cloudy = await renderer.render(
                tasks(12),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather("上海", "阴", "cloud", 12, 9, 15),
                battery_from_mv(None),
            )
            assert sunny.display_hash == cloudy.display_hash
        finally:
            await renderer.close()

    asyncio.run(run())


def test_footer_time_alone_does_not_change_display_hash():
    async def run():
        renderer = HtmlRenderer()
        try:
            first = await renderer.render(
                tasks(10),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            later = await renderer.render(
                tasks(10),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 15, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert first.layout == later.layout == "standard"
            assert first.display_hash == later.display_hash
            assert first.png != later.png
        finally:
            await renderer.close()

    asyncio.run(run())


def test_empty_table_renders_empty_layout():
    async def run():
        renderer = HtmlRenderer()
        try:
            frame = await renderer.render(
                [],
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert frame.layout == "empty"
        finally:
            await renderer.close()

    asyncio.run(run())


def test_renderer_recovers_after_browser_crash():
    async def run():
        renderer = HtmlRenderer()
        try:
            await renderer.start()
            await renderer._browser.close()
            frame = await renderer.render(
                tasks(5),
                date(2026, 11, 2),
                datetime(2026, 11, 2, 14, 30),
                Weather.unavailable("上海"),
                battery_from_mv(None),
            )
            assert frame.layout == "relaxed"
            assert frame.png
        finally:
            await renderer.close()

    asyncio.run(run())


def test_render_timeout_releases_the_browser(monkeypatch):
    async def run():
        renderer = HtmlRenderer()
        try:
            await renderer.start()
            renderer.render_timeout = 0.05

            async def hang(_document):
                await asyncio.sleep(30)

            monkeypatch.setattr(renderer, "_paint", hang)
            with pytest.raises(asyncio.TimeoutError):
                await renderer.render(
                    tasks(6),
                    date(2026, 11, 2),
                    datetime(2026, 11, 2, 14, 30),
                    Weather.unavailable("上海"),
                    battery_from_mv(None),
                )
            assert renderer._browser is None
        finally:
            await renderer.close()

    asyncio.run(run())
