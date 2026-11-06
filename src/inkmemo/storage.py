from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import re
import tempfile
import threading
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InkMemo"


_log = logging.getLogger("inkmemo.storage")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data)
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def protect_text(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for protected settings")
    source, source_buf = _blob(value.encode("utf-8"))
    output = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "InkMemo", None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(output.pbData, output.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buf


def unprotect_text(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for protected settings")
    encrypted = base64.b64decode(value)
    source, source_buf = _blob(encrypted)
    output = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del source_buf


def validate_wps_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("WPS 链接必须是 https:// 地址")
    host = parsed.hostname.lower()
    if not (host == "kdocs.cn" or host.endswith(".kdocs.cn") or host == "wps.cn" or host.endswith(".wps.cn")):
        raise ValueError("链接不是受支持的 WPS 文档域名")
    return value


@dataclass
class Settings:
    board_year: int | None = None
    city: str = "上海"
    latitude: float = 31.2304
    longitude: float = 121.4737
    interval_seconds: int = 1800
    near_days: int = 3
    paused: bool = False
    autostart: bool = False
    device_name: str = ""

    @classmethod
    def load(cls, base_dir: Path = APP_DIR) -> Settings:
        path = base_dir / "settings.json"
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = cls.__dataclass_fields__.keys()
        data = {key: value for key, value in raw.items() if key in allowed}
        for key in ("board_year", "interval_seconds", "near_days"):
            if data.get(key) is not None:
                data[key] = int(data[key])
        return cls(**data)

    def save(self, base_dir: Path = APP_DIR) -> None:
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(base_dir / "settings.json", payload)


class SecretStore:
    def __init__(self, base_dir: Path = APP_DIR) -> None:
        self.path = base_dir / "wps-url.bin"

    def set_url(self, url: str) -> None:
        value = validate_wps_url(url)
        _atomic_write(self.path, protect_text(value).encode("ascii"))

    def get_url(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return unprotect_text(self.path.read_text(encoding="ascii"))
        except (OSError, ValueError) as exc:
            _log.warning("secret decrypt failed: %s", type(exc).__name__)
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class RuntimeState:
    def __init__(self, base_dir: Path = APP_DIR) -> None:
        self.path = base_dir / "state.json"
        self.lock = threading.Lock()

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **changes: object) -> dict:
        with self.lock:
            state = self.read()
            state.update(changes)
            _atomic_write(self.path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))
            return state


def reset_source_cache(
    base_dir: Path = APP_DIR, state: RuntimeState | None = None
) -> None:
    preview_dir = base_dir / "previews"
    for name in ("last_good.png", "last_sent.png", "latest.png", "error.png"):
        (preview_dir / name).unlink(missing_ok=True)
    (state or RuntimeState(base_dir)).update(
        source_health="pending",
        last_task_count=0,
        last_layout=None,
        last_weather_ok=None,
        last_sent_display_hash=None,
        last_source_success_at=None,
        last_good_at=None,
        source_hash=None,
        last_error_class=None,
    )


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def redact(text: object) -> str:
    return URL_PATTERN.sub("<redacted-url>", str(text))
