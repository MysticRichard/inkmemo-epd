import pytest

from inkmemo.ble import lease


@pytest.fixture(autouse=True)
def isolate_ble_lease(tmp_path, monkeypatch):
    # Never let the test suite pause a user's real LCD controller.
    monkeypatch.setattr(lease, "KEY_PATH", tmp_path / "missing-ble-lease.key")
