from pathlib import Path

import pytest

from inkmemo.storage import (
    RuntimeState,
    SecretStore,
    Settings,
    protect_text,
    reset_source_cache,
    unprotect_text,
    validate_wps_url,
)


def test_dpapi_round_trip():
    secret = "https://www.kdocs.cn/l/example-private-value"
    assert unprotect_text(protect_text(secret)) == secret


def test_wps_url_validation():
    assert validate_wps_url("https://www.kdocs.cn/l/example") == "https://www.kdocs.cn/l/example"
    with pytest.raises(ValueError):
        validate_wps_url("http://www.kdocs.cn/l/example")
    with pytest.raises(ValueError):
        validate_wps_url("https://example.com/private")


def test_settings_round_trip(tmp_path: Path):
    settings = Settings(board_year=2026, city="上海", paused=True)
    settings.save(tmp_path)
    assert Settings.load(tmp_path) == settings
    store = SecretStore(tmp_path)
    store.set_url("https://www.kdocs.cn/l/example")
    assert store.get_url() == "https://www.kdocs.cn/l/example"
    store.clear()
    assert store.get_url() is None


def test_corrupt_secret_returns_none(tmp_path: Path):
    store = SecretStore(tmp_path)
    (tmp_path / "wps-url.bin").write_text("not-base64", encoding="ascii")

    assert store.get_url() is None


def test_reset_source_cache_clears_old_document_state(tmp_path: Path):
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    for name in ("last_good.png", "last_sent.png", "latest.png", "error.png"):
        (preview_dir / name).write_bytes(b"old")
    RuntimeState(tmp_path).update(
        source_health="ok",
        last_task_count=12,
        last_layout="standard",
        last_weather_ok=True,
        last_sent_display_hash="old",
        last_source_success_at="old",
        last_good_at="old",
        source_hash="old",
        last_error_class="auth",
    )

    reset_source_cache(tmp_path)

    assert not any((preview_dir / name).exists() for name in ("last_good.png", "last_sent.png", "latest.png", "error.png"))
    state = RuntimeState(tmp_path).read()
    assert state["source_health"] == "pending"
    assert state["last_task_count"] == 0
    assert state["last_layout"] is None
    assert state["last_weather_ok"] is None
    assert state["last_sent_display_hash"] is None
    assert state["last_source_success_at"] is None
    assert state["last_good_at"] is None
    assert state["source_hash"] is None
    assert state["last_error_class"] is None
