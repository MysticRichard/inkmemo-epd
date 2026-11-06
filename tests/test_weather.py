import asyncio

from inkmemo import weather


def test_reverse_geocode_prefers_city():
    assert weather._city_from_payload(
        {"city": "杭州市", "locality": "西湖区", "principalSubdivision": "浙江省"}
    ) == "杭州市"


def test_location_falls_back_to_configured_city(monkeypatch):
    async def unavailable():
        raise OSError("location unavailable")

    monkeypatch.setattr(weather, "_geocode_city", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(weather, "_windows_position", unavailable)
    location = asyncio.run(weather.resolve_weather_location("上海", 31.2304, 121.4737))

    assert location.city == "上海"
    assert location.latitude == 31.2304


def test_configured_city_wins_over_windows_position(monkeypatch):
    async def unexpected_position():
        raise AssertionError("configured city must not use Windows location")

    monkeypatch.setattr(weather, "_windows_position", unexpected_position)
    monkeypatch.setattr(
        weather,
        "_geocode_city",
        lambda city, *_args, **_kwargs: weather.WeatherLocation(city, 27.99, 120.7),
    )

    location = asyncio.run(weather.resolve_weather_location("杭州市", 30.2741, 120.1551))

    assert location.city == "杭州市"
    assert location.latitude == 27.99


def test_weather_network_failure_returns_unavailable(monkeypatch):
    def offline(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(weather._DIRECT_OPENER, "open", offline)

    result = weather.fetch_weather("上海", 31.2304, 121.4737)

    assert result.available is False
    assert result.city == "上海"
