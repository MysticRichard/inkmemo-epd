from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from ..models import Grid, MergeRange
from ..storage import APP_DIR, redact


class WpsSourceError(RuntimeError):
    pass


class WpsAuthError(WpsSourceError):
    pass


DIRECT_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-proxy-server",
    "--proxy-bypass-list=*",
]


READ_GRID_JS = r"""
async () => {
  const api = window.WPSOpenApi;
  if (!api) throw new Error("WPS_OPEN_API_MISSING");
  await api.ready();
  const root = api.EtApplication();
  const app = root && (root.__proxy_ref__ || root);
  if (!app || !app.ActiveWorkbook) throw new Error("WPS_WORKBOOK_MISSING");
  const sheet = app.ActiveWorkbook.ActiveSheet;
  const used = await sheet.UsedRange;
  const raw = await used.Value;
  if (!Array.isArray(raw)) throw new Error("WPS_RANGE_MISSING");
  const rowBase = Number(used.Row);
  const colBase = Number(used.Column);
  const rowOffset = rowBase - 1;
  const colOffset = colBase - 1;
  const rowCount = raw.length;
  const colCount = Math.max(0, ...raw.map((row) => row.length));
  const values = Array.from({ length: rowCount }, (_, r) =>
    Array.from({ length: colCount }, (_, c) => raw[r] && raw[r][c] != null ? raw[r][c] : "")
  );
  const merges = [];
  for (let r = 0; r < rowCount; r += 1) {
    for (let c = 0; c < colCount; c += 1) {
      let found = [];
      try {
        found = await sheet.GetMergeCells({ Row: rowOffset + r, Col: colOffset + c });
      } catch (_) {}
      for (const merge of found || []) {
        const item = {
          row_from: Number(merge.rowFrom),
          row_to: Number(merge.rowTo),
          col_from: Number(merge.colFrom),
          col_to: Number(merge.colTo),
        };
        if (!merges.some((other) => JSON.stringify(other) === JSON.stringify(item))) {
          merges.push(item);
        }
      }
    }
  }
  return {
    values,
    merges,
    rowOffset,
    colOffset,
    sheetName: String(sheet.Name || ""),
  };
}
"""


def _relative_index(value: int, offset: int) -> int:
    return value - offset


class WpsReader:
    def __init__(self, profile_dir: Path | None = None) -> None:
        self.profile_dir = profile_dir or APP_DIR / "browser-profile"
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._context is not None:
            browser = self._context.browser
            alive = (
                browser is not None
                and browser.is_connected()
                and self._page is not None
                and not self._page.is_closed()
            )
            if alive:
                return
            # A crashed Edge leaves the handle set but dead; without this the
            # background service would fail every cycle and never recover.
            await self.close()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            channel="msedge",
            headless=True,
            viewport={"width": 1280, "height": 800},
            args=DIRECT_BROWSER_ARGS,
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._playwright = None

    async def login(self, url: str) -> None:
        async with self._lock:
            await self.close()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            playwright = await async_playwright().start()
            context: BrowserContext | None = None
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    channel="msedge",
                    headless=False,
                    viewport={"width": 1280, "height": 800},
                    args=DIRECT_BROWSER_ARGS,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                closed = asyncio.Event()
                page.on("close", lambda _page: closed.set())
                context.on("close", lambda _context: closed.set())
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except Exception:
                    pass
                await closed.wait()
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                await playwright.stop()

    async def read(self, url: str) -> Grid:
        async with self._lock:
            await self.start()
            assert self._page is not None
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await self._page.wait_for_function(
                    "() => Boolean(window.WPSOpenApi && window.WPSOpenApi.ready)",
                    timeout=45_000,
                )
                data = await asyncio.wait_for(self._page.evaluate(READ_GRID_JS), 120)
            except asyncio.TimeoutError as exc:
                # A hung page would stall the background loop for good.
                await self.close()
                raise WpsSourceError("WPS 云端读取超时，已重置浏览器") from exc
            except Exception as exc:
                message = redact(exc)
                if "Timeout" in message or "WPS_OPEN_API" in message:
                    raise WpsAuthError("WPS 页面未就绪，可能需要重新登录或检查链接权限") from exc
                raise WpsSourceError(f"WPS 云端读取失败：{message}") from exc
            values = data.get("values")
            if not isinstance(values, list) or not values:
                raise WpsSourceError("WPS 工作表为空或读取范围不可用")
            merges = [
                MergeRange(
                    row_from=_relative_index(int(item["row_from"]), int(data["rowOffset"])),
                    row_to=_relative_index(int(item["row_to"]), int(data["rowOffset"])),
                    col_from=_relative_index(int(item["col_from"]), int(data["colOffset"])),
                    col_to=_relative_index(int(item["col_to"]), int(data["colOffset"])),
                )
                for item in data.get("merges", [])
            ]
            merges = [
                merge
                for merge in merges
                if merge.row_from >= 0
                and merge.col_from >= 0
                and merge.row_to < len(values)
                and merge.col_to < max((len(row) for row in values), default=0)
            ]
            return Grid(
                values=values,
                merges=merges,
                document_title=await self._page.title(),
                sheet_name=str(data.get("sheetName") or ""),
                fetched_at=datetime.now().astimezone(),
            )
