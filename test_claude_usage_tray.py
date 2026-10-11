"""Tests for the logic behind the tray widget. Nothing here touches the network,
the real credentials file, the real settings folder or the real startup key."""

import json
import winreg
from datetime import datetime, timedelta, timezone

import pytest
import requests

import claude_usage_tray as app

NOW = datetime(2026, 10, 11, 9, 15, tzinfo=timezone.utc)

# Trimmed copy of a real reply from the usage endpoint (October 2026).
PAYLOAD = {
    "five_hour": {"utilization": 57.0, "resets_at": "2026-10-11T11:29:59.747374+00:00"},
    "seven_day": {"utilization": 15.0, "resets_at": "2026-10-17T06:59:59.747396+00:00"},
    "seven_day_opus": None,
    "iguana_necktie": {"utilization": 0.0, "resets_at": "2026-11-05T07:59:00+00:00", "limit_dollars": 250},
    "extra_usage": {"is_enabled": False, "utilization": None},
    "limits": [
        {"kind": "session", "percent": 57, "resets_at": "2026-10-11T11:29:59.747374+00:00", "scope": None},
        {"kind": "weekly_all", "percent": 15, "resets_at": "2026-10-17T06:59:59.747396+00:00", "scope": None},
        {
            "kind": "weekly_scoped",
            "percent": 0,
            "resets_at": "2026-10-17T07:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}


def usage_with(five=57.0, seven=15.0, plan="Max 20x"):
    return app.Usage(
        NOW,
        (
            app.Limit("five_hour", "5-hour session", five, NOW + timedelta(hours=2, minutes=15)),
            app.Limit("seven_day", "7-day (all models)", seven, NOW + timedelta(days=5, hours=21)),
        ),
        plan,
    )


def write_credentials(path, expires_at, token="token-value", **oauth):
    oauth = {"accessToken": token, "expiresAt": int(expires_at.timestamp() * 1000), **oauth}
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


# -- usage parsing -----------------------------------------------------------


def test_parse_usage_reads_the_real_reply_shape():
    usage = app.parse_usage(PAYLOAD, NOW, "Max 20x")

    assert [(limit.key, limit.label, limit.percent) for limit in usage.limits] == [
        ("five_hour", "5-hour session", 57.0),
        ("seven_day", "7-day (all models)", 15.0),
        ("weekly_scoped:Fable", "7-day Fable", 0.0),
    ]
    assert usage.get("five_hour").resets_at == datetime(2026, 10, 11, 11, 29, 59, 747374, tzinfo=timezone.utc)
    assert usage.plan == "Max 20x"


def test_parse_usage_never_shows_internal_codenames():
    usage = app.parse_usage(PAYLOAD, NOW)

    assert not [limit for limit in usage.limits if "iguana" in limit.label.lower()]


def test_parse_usage_handles_the_older_reply_without_a_limits_list():
    payload = {
        "seven_day_opus": {"utilization": 12, "resets_at": None},
        "seven_day": {"utilization": 40.5, "resets_at": "2026-10-17T07:00:00Z"},
        "five_hour": {"utilization": 3, "resets_at": None},
        "tangelo": {"utilization": 99, "resets_at": None},
    }

    usage = app.parse_usage(payload, NOW)

    assert [(limit.key, limit.percent) for limit in usage.limits] == [
        ("five_hour", 3.0),
        ("seven_day", 40.5),
        ("seven_day_opus", 12.0),
    ]
    assert usage.get("seven_day").resets_at == datetime(2026, 10, 17, 7, tzinfo=timezone.utc)
    assert usage.get("five_hour").resets_at is None


def test_parse_usage_does_not_duplicate_per_model_limits_present_in_both_shapes():
    payload = {**PAYLOAD, "seven_day_opus": {"utilization": 9, "resets_at": None}}

    usage = app.parse_usage(payload, NOW)

    assert usage.get("seven_day_opus") is None
    assert len(usage.limits) == 3


def test_parse_usage_fills_a_main_limit_the_list_lacks_from_the_top_level():
    payload = {**PAYLOAD, "limits": [PAYLOAD["limits"][1]]}

    usage = app.parse_usage(payload, NOW)

    assert [limit.key for limit in usage.limits] == ["five_hour", "seven_day"]


def test_parse_usage_survives_an_empty_or_odd_reply():
    assert app.parse_usage({}, NOW).limits == ()
    assert app.parse_usage({"limits": "nope", "five_hour": {"utilization": True}}, NOW).limits == ()


# -- formatting --------------------------------------------------------------


@pytest.mark.parametrize(
    "percent, expected",
    [(0, "ok"), (69.9, "ok"), (70, "warn"), (90, "warn"), (90.1, "crit"), (100, "crit"), (140, "crit")],
)
def test_severity_thresholds(percent, expected):
    assert app.severity(percent) == expected


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=-5), "now"),
        (timedelta(seconds=20), "1m"),
        (timedelta(minutes=45), "45m"),
        (timedelta(hours=2, minutes=5), "2h 05m"),
        (timedelta(days=3, hours=4, minutes=30), "3d 4h"),
    ],
)
def test_format_countdown(delta, expected):
    assert app.format_countdown(NOW + delta, NOW) == expected


def test_format_countdown_without_a_time():
    assert app.format_countdown(None, NOW) == ""


def test_describe_reset_names_the_day_and_clock_time():
    local_noon = datetime(2026, 10, 11, 12, 0).astimezone()
    today = app.Limit("five_hour", "5-hour session", 10, local_noon + timedelta(hours=2))
    tomorrow = app.Limit("five_hour", "5-hour session", 10, local_noon + timedelta(hours=26))

    assert app.describe_reset(today, local_noon, twelve_hour=False) == "Resets in 2h 00m (today 14:00)"
    assert app.describe_reset(today, local_noon, twelve_hour=True) == "Resets in 2h 00m (today 2:00 PM)"
    assert app.describe_reset(tomorrow, local_noon, twelve_hour=False) == "Resets in 1d 2h (tomorrow 14:00)"


def test_describe_reset_without_a_window_or_after_it_passed():
    idle = app.Limit("five_hour", "5-hour session", 0, None)
    passed = app.Limit("five_hour", "5-hour session", 80, NOW - timedelta(minutes=1))

    assert app.describe_reset(idle, NOW, False) == "No active window"
    assert "waiting for fresh data" in app.describe_reset(passed, NOW, False)


def test_reset_on_the_hour_is_not_shown_a_minute_early():
    local_noon = datetime(2026, 10, 11, 12, 0).astimezone()
    on_the_hour = app.Limit("seven_day", "7-day", 10, local_noon + timedelta(hours=2, seconds=-0.3))

    assert app.describe_reset(on_the_hour, local_noon, twelve_hour=False) == "Resets in 2h 00m (today 14:00)"


def test_tooltip_shows_both_limits_at_a_glance():
    assert app.build_tooltip(usage_with(), None, NOW) == (
        "Claude Usage (Max 20x)\n5h: 57% - resets in 2h 15m\n7d: 15% - resets in 5d 21h"
    )


def test_tooltip_before_the_first_reading_and_on_error():
    assert app.build_tooltip(None, None, NOW) == "Claude Usage\nLoading..."
    assert app.build_tooltip(None, "Not signed in", NOW) == "Claude Usage\nError: Not signed in"


def test_tooltip_keeps_the_last_reading_and_the_whole_reason_when_a_poll_fails():
    reason = "Sign-in token expired - it renews the next time Claude Code runs"

    tooltip = app.build_tooltip(usage_with(), reason, NOW)

    assert tooltip == f"Claude Usage (Max 20x)\n5h: 57% | 7d: 15% (last reading)\n{reason}"
    assert len(tooltip) <= 127


def test_tooltip_never_exceeds_the_windows_limit():
    # pystray copies the title into a 128-character buffer and raises if it does not fit
    assert len(app.build_tooltip(usage_with(), "x" * 500, NOW)) <= 127


# -- credentials -------------------------------------------------------------


def test_read_credentials_file(tmp_path):
    path = write_credentials(
        tmp_path / "c.json", NOW + timedelta(hours=1), subscriptionType="max", rateLimitTier="default_claude_max_20x"
    )

    creds = app.read_credentials_file(path)

    assert creds.access_token == "token-value"
    assert creds.plan == "Max 20x"
    assert creds.expires_at == NOW + timedelta(hours=1)
    assert not creds.is_expired(NOW)
    assert "token-value" not in repr(creds)


@pytest.mark.parametrize("content", ["", "not json", "[]", "{}", '{"claudeAiOauth": {"accessToken": ""}}'])
def test_read_credentials_file_rejects_unusable_files(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")

    assert app.read_credentials_file(path) is None
    assert app.read_credentials_file(tmp_path / "missing.json") is None


def test_plan_label():
    assert app.plan_label({"subscriptionType": "pro"}) == "Pro"
    assert app.plan_label({"subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}) == "Max 5x"
    assert app.plan_label({}) == ""


def test_load_credentials_prefers_windows_and_leaves_wsl_alone(tmp_path, monkeypatch):
    windows = write_credentials(tmp_path / "win.json", NOW + timedelta(hours=1), token="windows")
    monkeypatch.setattr(app, "windows_credential_paths", lambda: [windows])
    monkeypatch.setattr(app, "wsl_credential_paths", lambda: pytest.fail("WSL must not be woken up"))

    assert app.load_credentials(NOW).access_token == "windows"


def test_load_credentials_falls_back_to_wsl_when_windows_token_expired(tmp_path, monkeypatch):
    windows = write_credentials(tmp_path / "win.json", NOW - timedelta(hours=1), token="windows")
    wsl = write_credentials(tmp_path / "wsl.json", NOW + timedelta(hours=1), token="wsl")
    monkeypatch.setattr(app, "windows_credential_paths", lambda: [windows])
    monkeypatch.setattr(app, "wsl_credential_paths", lambda: [wsl])

    assert app.load_credentials(NOW).access_token == "wsl"


def test_load_credentials_reports_expired_and_missing_tokens(tmp_path, monkeypatch):
    expired = write_credentials(tmp_path / "win.json", NOW - timedelta(hours=1))
    monkeypatch.setattr(app, "wsl_credential_paths", lambda: [])

    monkeypatch.setattr(app, "windows_credential_paths", lambda: [expired])
    with pytest.raises(app.UsageError, match="expired"):
        app.load_credentials(NOW)

    monkeypatch.setattr(app, "windows_credential_paths", lambda: [tmp_path / "missing.json"])
    with pytest.raises(app.UsageError, match="Not signed in"):
        app.load_credentials(NOW)


# -- usage request -----------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def creds():
    return app.Credentials("secret-token", None, "Max", app.Path("c.json"))


def test_request_usage_sends_the_claude_code_token_only_to_anthropic(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout, allow_redirects):
        calls.append((url, headers, allow_redirects))
        return FakeResponse(payload=PAYLOAD)

    monkeypatch.setattr(app.requests, "get", fake_get)

    assert app.request_usage(creds()) == PAYLOAD
    (url, headers, allow_redirects), = calls
    assert url == "https://api.anthropic.com/api/oauth/usage"
    assert allow_redirects is False
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.parametrize(
    "response, message, transient, retry_after",
    [
        (FakeResponse(401), "rejected", False, None),
        (FakeResponse(429, headers={"Retry-After": "900"}), "Rate limited", False, 900),
        (FakeResponse(429), "Rate limited", False, app.RATE_LIMIT_BACKOFF_S),
        (FakeResponse(503), "HTTP 503", True, None),
        (FakeResponse(403), "HTTP 403", False, None),
        (FakeResponse(302, headers={"Location": "https://elsewhere.example/"}), "HTTP 302", False, None),
        (FakeResponse(200, payload=None), "unreadable", False, None),
        (FakeResponse(200, payload=[1]), "unexpected", False, None),
    ],
)
def test_request_usage_maps_failures_to_short_messages(monkeypatch, response, message, transient, retry_after):
    monkeypatch.setattr(app.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises(app.UsageError, match=message) as raised:
        app.request_usage(creds())

    assert raised.value.transient is transient
    assert raised.value.retry_after_s == retry_after
    assert "secret-token" not in str(raised.value)


def test_request_usage_treats_network_errors_as_transient(monkeypatch):
    def offline(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(app.requests, "get", offline)

    with pytest.raises(app.UsageError, match="Network error") as raised:
        app.request_usage(creds())

    assert raised.value.transient


# -- settings, history, startup ----------------------------------------------


def test_config_round_trip_and_clamping(tmp_path):
    path = tmp_path / "nested" / "config.json"
    assert app.Config.load(path).refresh_minutes == app.DEFAULT_REFRESH_MINUTES

    config = app.Config(refresh_minutes=10, path=path)
    config.save()
    assert app.Config.load(path).refresh_minutes == 10

    for stored, expected in ((0, 3), (1, 3), (9999, 120), ("abc", app.DEFAULT_REFRESH_MINUTES)):
        path.write_text(json.dumps({"refresh_minutes": stored}), encoding="utf-8")
        assert app.Config.load(path).refresh_minutes == expected


def test_config_remembers_the_taskbar_strip_settings(tmp_path):
    path = tmp_path / "config.json"
    config = app.Config(path=path)
    assert config.taskbar_strip and config.strip_gap_primary is None

    config.taskbar_strip, config.strip_gap_secondary = False, 250.5
    config.save()
    loaded = app.Config.load(path)

    assert (loaded.taskbar_strip, loaded.strip_gap_primary, loaded.strip_gap_secondary) == (False, None, 250.5)


def test_config_ignores_values_of_the_wrong_type(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"taskbar_strip": "yes", "strip_gap_primary": "far", "refresh_minutes": True}))

    loaded = app.Config.load(path)

    assert (loaded.taskbar_strip, loaded.strip_gap_primary, loaded.refresh_minutes) == (
        True,
        None,
        app.DEFAULT_REFRESH_MINUTES,
    )


def test_history_survives_a_restart_and_filters_by_time(tmp_path):
    path = tmp_path / "history.json"
    history = app.History(path)
    old = app.Usage(NOW - timedelta(hours=30), usage_with(five=10).limits)
    history.add(old)
    history.add(usage_with(five=57, seven=15))

    reloaded = app.History(path)

    assert len(reloaded.samples) == 2
    assert reloaded.since(NOW - timedelta(hours=24)) == [(NOW.timestamp(), 57, 15)]


def test_history_ignores_a_corrupt_file(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{broken", encoding="utf-8")

    assert len(app.History(path).samples) == 0


def test_startup_toggle_writes_and_removes_the_run_value():
    key_path, name = r"Software\ClaudeUsageTrayTest", "ClaudeUsageTrayTest"
    try:
        assert not app.is_startup_enabled(key_path, name)

        app.set_startup(True, key_path, name)
        assert app.is_startup_enabled(key_path, name)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            command = winreg.QueryValueEx(key, name)[0]
        assert command == app.startup_command()
        assert "claude_usage_tray.py" in command and "pythonw.exe" in command

        app.set_startup(False, key_path, name)
        assert not app.is_startup_enabled(key_path, name)
        app.set_startup(False, key_path, name)  # turning it off twice is harmless
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


# -- drawing -----------------------------------------------------------------


def rgb(hex_color):
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


LEFT_BAR, RIGHT_BAR = 16, 46  # x in the middle of each gauge of the 64 px icon


def is_empty_track(pixel):
    """Translucent grey; resampling may shift a channel by one."""
    return pixel[3] == app.ICON_TRACK[3] and all(abs(a - b) <= 2 for a, b in zip(pixel, app.ICON_TRACK))


@pytest.mark.parametrize("percent, status", [(50, "ok"), (80, "warn"), (95, "crit")])
def test_icon_gauges_are_colored_by_their_own_severity(percent, status):
    icon = app.render_icon(usage_with(five=percent, seven=10))

    assert icon.size == (64, 64) and icon.mode == "RGBA"
    assert icon.getpixel((LEFT_BAR, 54))[:3] == rgb(app.STATUS_COLORS[status])
    assert icon.getpixel((RIGHT_BAR, 54))[:3] == rgb(app.STATUS_COLORS["ok"])


def test_icon_gauge_height_follows_the_percentage():
    icon = app.render_icon(usage_with(five=50, seven=100))

    assert is_empty_track(icon.getpixel((LEFT_BAR, 12)))  # upper half of a 50 % gauge
    assert icon.getpixel((RIGHT_BAR, 12))[:3] == rgb(app.STATUS_COLORS["crit"])


def test_icon_goes_grey_when_stale_and_empty_without_data():
    stale = app.render_icon(usage_with(five=95), stale=True)
    empty = app.render_icon(None)

    assert stale.getpixel((LEFT_BAR, 54))[:3] == rgb(app.ICON_STALE)
    assert is_empty_track(empty.getpixel((LEFT_BAR, 54)))


def test_chart_line_breaks_across_long_gaps():
    points = [(0, 1), (60, 2), (60 + app.HISTORY_GAP_S + 1, 3)]

    assert app._runs(points) == [[(0, 1), (60, 2)], [(60 + app.HISTORY_GAP_S + 1, 3)]]


def test_bar_and_chart_images_have_the_requested_size():
    bar = app.bar_image(300, 8, 57, app.STATUS_COLORS["ok"], app.DARK.track, app.DARK.bg)
    samples = [(NOW.timestamp() - 600, 10.0, 5.0), (NOW.timestamp(), 57.0, 15.0)]
    chart = app.history_image(300, 56, samples, NOW.timestamp() - 86400, NOW.timestamp(), app.DARK)

    assert bar.size == (300, 8)
    assert bar.getpixel((20, 4)) == rgb(app.STATUS_COLORS["ok"])
    assert bar.getpixel((280, 4)) == rgb(app.DARK.track)
    assert chart.size == (300, 56)


# -- taskbar strip -----------------------------------------------------------

# The two taskbars of a real setup: a 2560x1600 laptop screen at 150 % and a 1920x1080 monitor at 100 %.
MAIN_BAR = app.Taskbar(hwnd=1, primary=True, rect=(0, 1528, 2560, 1600), anchor=2125, scale=1.5)
SECOND_BAR = app.Taskbar(hwnd=2, primary=False, rect=(2560, 1032, 4480, 1080), anchor=4396, scale=1.0)


def has_color(image, hex_color, tolerance=12):
    """Some nearly opaque pixel is that color, give or take resampling."""
    wanted = rgb(hex_color)
    return any(
        pixel[3] > 240 and all(abs(a - b) <= tolerance for a, b in zip(pixel, wanted))
        for _, pixel in image.getcolors(maxcolors=image.width * image.height)
    )


def with_models(usage, **percent_by_model):
    """The same reading on a plan that also has per-model weekly limits."""
    extras = tuple(
        app.Limit(f"weekly_scoped:{name}", f"7-day {name}", percent, NOW + timedelta(days=5))
        for name, percent in percent_by_model.items()
    )
    return app.Usage(usage.fetched_at, usage.limits + extras, usage.plan)


def test_strip_sits_just_left_of_the_clock_area_on_each_monitor():
    assert app.strip_geometry(MAIN_BAR, None, 68) == (2017, 1528, 102, 72)
    assert app.strip_geometry(SECOND_BAR, None, 68) == (4320, 1032, 68, 48)


def test_dragged_strip_position_round_trips_and_stays_on_its_taskbar():
    x, _, width, _ = app.strip_geometry(MAIN_BAR, 300, 68)

    assert app.strip_gap(MAIN_BAR, x, width) == 300
    assert app.strip_geometry(SECOND_BAR, 99999, 68)[0] == 2560  # clamped to the left end
    assert app.strip_geometry(SECOND_BAR, -99999, 68)[0] == 4480 - 68  # and to the right end


def test_strip_shrinks_to_fit_a_short_taskbar():
    windows_10_bar = app.Taskbar(hwnd=3, primary=True, rect=(0, 1040, 1920, 1080), anchor=1700, scale=1.0)

    assert app.strip_unit(windows_10_bar) == pytest.approx(40 / 44)
    assert app.strip_geometry(windows_10_bar, None, 68)[2:] == (round(68 * 40 / 44), 40)


def test_readout_lists_both_main_limits_then_the_fullest_model_limit():
    plain = usage_with(five=59, seven=16)

    assert [label for label, _ in app.readout_rows(plain)] == ["5h", "7d"]
    assert [label for label, _ in app.readout_rows(with_models(plain, Fable=0))] == ["5h", "7d", "Fable"]
    crowded = app.readout_rows(with_models(plain, Opus=20, Sonnet=85))
    assert [(label, limit.percent) for label, limit in crowded] == [("5h", 59), ("7d", 16), ("Sonnet", 85)]
    assert app.readout_rows(None) == [("5h", None), ("7d", None)]


def test_readout_is_only_as_wide_as_its_longest_name_needs():
    plain = usage_with()

    narrow, wide = app.strip_design_width(plain), app.strip_design_width(with_models(plain, Fable=0))

    assert narrow < wide < 110
    assert app.strip_design_width(None) == narrow  # before the first reading: just the two main lines
    # one digit or two makes no difference (no twitch at 10 %); only a 100 % needs more room
    assert app.strip_design_width(usage_with(five=5)) == narrow < app.strip_design_width(usage_with(five=100))


def gauge_rows(image, color):
    """The y of every pixel row where a gauge of that color is drawn."""
    wanted = rgb(color)
    return sorted(
        {
            y
            for y in range(image.height)
            for x in range(image.width)
            if (pixel := image.getpixel((x, y)))[3] > 240 and all(abs(a - b) <= 12 for a, b in zip(pixel, wanted))
        }
    )


def test_readout_puts_name_gauge_and_percentage_on_one_line_each():
    usage = with_models(usage_with(five=59, seven=80), Fable=96)
    width = round(app.strip_design_width(usage) * 1.5)

    image = app.render_strip(width, 72, 1.5, usage, stale=False, dark=False)

    lines = [gauge_rows(image, app.STATUS_COLORS[status]) for status in ("ok", "warn", "crit")]
    assert all(lines), "one gauge per line: green 59 %, yellow 80 %, red 96 %"
    assert lines[0][-1] < lines[1][0] and lines[1][-1] < lines[2][0]  # stacked top to bottom, not overlapping
    assert lines[0][0] > 0 and lines[2][-1] < 72  # all three inside the taskbar's height
    # on the gauge's own pixel row there is text to its left (the name) and to its right (the percentage)
    y = lines[0][len(lines[0]) // 2]
    green = rgb(app.STATUS_COLORS["ok"])
    gauge = [x for x in range(width) if all(abs(a - b) <= 12 for a, b in zip(image.getpixel((x, y)), green))]
    solid = [x for x in range(width) if image.getpixel((x, y))[3] > 200 and x not in gauge]
    assert any(x < gauge[0] for x in solid) and any(x > gauge[-1] for x in solid)


@pytest.mark.parametrize("dark", [False, True])
def test_strip_image_is_clickable_everywhere_and_colored_by_severity(dark):
    image = app.render_strip(102, 72, 1.5, usage_with(five=59, seven=96), stale=False, dark=dark)

    assert image.size == (102, 72) and image.mode == "RGBA"
    assert image.getchannel("A").getextrema()[0] >= 1  # alpha 0 would let clicks fall through to the taskbar
    assert has_color(image, app.STATUS_COLORS["ok"])
    assert has_color(image, app.STATUS_COLORS["crit"])
    assert has_color(image, app.STRIP_INK[dark][0])  # the numbers


def test_strip_image_survives_being_too_narrow_for_gauges():
    image = app.render_strip(40, 72, 1.5, usage_with(), stale=False, dark=True)

    assert image.size == (40, 72)


def test_strip_image_drops_the_status_colors_when_stale_or_empty():
    stale = app.render_strip(102, 72, 1.5, usage_with(five=59, seven=96), stale=True, dark=True)
    empty = app.render_strip(102, 72, 1.5, None, stale=False, dark=True)

    assert has_color(stale, app.ICON_STALE)
    for image in (stale, empty):
        assert not any(has_color(image, color) for color in app.STATUS_COLORS.values())
        assert not has_color(image, app.STRIP_INK[True][0])  # numbers fall back to the quieter label ink


def test_strip_mouse_click_opens_details_and_drag_moves_and_remembers(tmp_path):
    config = app.Config(path=tmp_path / "config.json")
    seen = []
    strip = app.TaskbarStrip(config, on_click=lambda: seen.append("click"), on_menu=lambda: seen.append("menu"))
    overlay = app._Overlay(hwnd=0, bar=SECOND_BAR, x=4320, width=68)
    cursor = iter([4350, 4352, 4250, 4250, 4250])
    strip._cursor_x = lambda: next(cursor)

    strip._handle_mouse(overlay, app.WM_LBUTTONDOWN)
    strip._handle_mouse(overlay, app.WM_MOUSEMOVE)  # 2 px: a shaky click, not a drag
    assert (overlay.x, overlay.dragged) == (4320, False)
    strip._handle_mouse(overlay, app.WM_MOUSEMOVE)  # 100 px to the left
    assert (overlay.x, overlay.dragged) == (4220, True)
    strip._handle_mouse(overlay, app.WM_LBUTTONUP)
    strip._handle_mouse(overlay, app.WM_CAPTURECHANGED)  # Windows sends this when the mouse is released

    assert seen == []  # a drag is not a click
    assert app.Config.load(config.path).strip_gap_secondary == 108
    assert app.strip_geometry(SECOND_BAR, config.strip_gap_secondary, 68)[0] == 4220
    assert overlay.drag is None

    strip._handle_mouse(overlay, app.WM_LBUTTONDOWN)
    strip._handle_mouse(overlay, app.WM_LBUTTONUP)
    strip._handle_mouse(overlay, app.WM_RBUTTONUP)
    assert seen == ["click", "menu"]


# -- mascot --------------------------------------------------------------------

import random  # noqa: E402

# The Claude logo on its 16 x 10 grid: # body, o eye, . nothing.
LOGO = (
    "..############..",
    "..############..",
    "..##o######o##..",
    "..##o######o##..",
    "################",
    "################",
    "..############..",
    "..############..",
    "...#.#....#.#...",
    "...#.#....#.#...",
)


def mascot_cells(pose, unit=2):
    """The creature part of a frame, read back as rows of # o . like LOGO."""
    image = app.render_mascot(unit, pose)
    left, top = app.MASCOT_ORIGIN
    marks = {rgb(app.MASCOT_BODY): "#", rgb(app.MASCOT_EYE): "o"}
    rows = []
    for row in range(10):
        pixels = (image.getpixel(((left + column) * unit, (top + row) * unit)) for column in range(16))
        rows.append("".join(marks.get(pixel[:3], "?") if pixel[3] else "." for pixel in pixels))
    return tuple(rows)


def test_mascot_standing_still_is_exactly_the_claude_logo():
    assert mascot_cells(app.Pose()) == LOGO
    assert mascot_cells(app.Pose(), unit=3) == LOGO
    assert app.render_mascot(3, app.Pose()).size == (app.MASCOT_CANVAS[0] * 3, app.MASCOT_CANVAS[1] * 3)


@pytest.mark.parametrize(
    "five, seven, mood",
    [
        (5, 10, "sleepy"),
        (30, 10, "calm"),
        (10, 55, "busy"),  # the fuller of the two limits decides
        (70, 10, "nervous"),
        (79, 10, "nervous"),
        (80, 10, "stressed"),
        (90, 10, "stressed"),
        (91, 10, "panic"),
        (20, 100, "dead"),
    ],
)
def test_mascot_mood_follows_the_fuller_limit(five, seven, mood):
    assert app.mascot_mood(usage_with(five=five, seven=seven), stale=False) == mood


def test_mascot_frets_about_a_model_limit_too():
    usage = with_models(usage_with(five=10, seven=10), Fable=95)

    assert app.mascot_mood(usage, stale=False) == "panic"


def test_tooltip_lists_the_model_limit_as_well():
    tooltip = app.build_tooltip(with_models(usage_with(), Fable=3), None, NOW)

    assert tooltip.splitlines()[-1] == "Fable: 3% - resets in 5d 0h"
    assert len(tooltip) <= 127


def test_mascot_is_confused_without_a_trustworthy_reading():
    assert app.mascot_mood(None, False) == "confused"
    assert app.mascot_mood(usage_with(five=95), stale=True) == "confused"
    assert app.mascot_pose("confused", "rest", 5, 1).sign == "?"


def test_mascot_sweats_more_the_closer_the_limit_gets():
    sweat = [app.mascot_pose(mood, "walk", 7, 1).sweat for mood in ("sleepy", "calm", "busy", "nervous", "stressed", "panic")]

    assert sweat == sorted(sweat) and sweat[0] == 0 and sweat[-1] == len(app.SWEAT_PATHS)
    speeds = [app.MOODS[mood].speed for mood in ("sleepy", "calm", "busy", "nervous", "stressed", "panic")]
    assert speeds == sorted(speeds)  # and scuttles faster


def test_mascot_poses_tell_the_moods_apart():
    panic = app.mascot_pose("panic", "walk", 3, -1)
    asleep = app.mascot_pose("sleepy", "rest", 15, 1)
    fainted = app.mascot_pose("dead", "rest", 400, 1)  # long after the fall

    assert (panic.eyes, panic.mouth, panic.look) == ("wide", True, -1)
    assert (asleep.legs, asleep.eyes, asleep.sign) == (3, "shut", "z")  # sitting down, snoring
    assert fainted.upside_down and fainted.eyes == "cross"
    assert mascot_cells(fainted)[0] == LOGO[9]  # on its back: the legs are on top
    assert app.mascot_pose("calm", "walk", 0, 1).legs != app.mascot_pose("calm", "walk", 4, 1).legs  # it steps


def test_mascot_sweat_drops_are_drawn_in_the_air_above_it():
    image = app.render_mascot(2, app.mascot_pose("panic", "walk", 3, 1))
    top_of_head = app.MASCOT_ORIGIN[1] * 2

    assert any(pixel[:3] == rgb(app.MASCOT_SWEAT) for _, pixel in image.crop((0, 0, 48, top_of_head)).getcolors(4096))


PEN = (4152.0, 4276.0)  # the mascot's stretch of the second taskbar, see the pen tests below


def walk(mood, seconds):
    pet = app._Pet(hwnd=0, bar=SECOND_BAR, x=4200.0)
    rng, now, xs = random.Random(3), 0.0, []
    for _ in range(int(seconds / 0.07)):
        now += 0.07
        app.step_pet(pet, mood, now, 0.07, rng, PEN)
        xs.append(pet.x)
    return pet, xs


def test_mascot_stays_in_its_pen_and_uses_all_of_it():
    _, xs = walk("panic", seconds=120)

    assert (min(xs), max(xs)) == PEN


def test_mascot_is_pulled_back_when_its_pen_moves():
    pet = app._Pet(hwnd=0, bar=SECOND_BAR, x=4200.0)

    app.step_pet(pet, "dead", 1.0, 0.07, random.Random(1), (3000.0, 3100.0))  # even though it is out cold

    assert pet.x == 3100.0


def test_mascot_pen_is_the_free_taskbar_left_of_the_readout():
    from dataclasses import replace

    main_strip, second_strip = app.strip_geometry(MAIN_BAR, None, 68), app.strip_geometry(SECOND_BAR, None, 68)
    crowded = replace(MAIN_BAR, icons_end=1945)  # app buttons end 72 px short of the readout at 2017
    roomy = replace(SECOND_BAR, icons_end=3721)

    low, high, y = app.mascot_pen(crowded, main_strip[0], main_strip[2], above=False)
    assert (low, high) == (2017 - 72 - 12, 2017 - 6 - 48 - 12)  # the 72 free pixels, a gap before the readout
    packed = replace(MAIN_BAR, icons_end=2010)  # no room at all: it stands still against the readout
    assert app.mascot_pen(packed, main_strip[0], main_strip[2], above=False)[:2] == (2017 - 54 - 12, 2017 - 54 - 12)
    assert y + app.MASCOT_CANVAS[1] * 3 == 1600 - 6  # feet a little above the bottom edge, inside the taskbar
    assert y >= 1528

    assert app.mascot_pen(roomy, second_strip[0], second_strip[2], above=False) == (4320 - 160 - 8, 4320 - 4 - 32 - 8, 1080 - 4 - 42)
    assert app.mascot_pen(SECOND_BAR, second_strip[0], second_strip[2], above=False)[:2] == PEN  # unknown: assume the maximum


def test_mascot_above_the_taskbar_paces_only_over_the_readout():
    x, top, width, _ = app.strip_geometry(MAIN_BAR, None, 91)  # the three-line readout: 136 px at 150 %
    unit = app.mascot_unit(MAIN_BAR)  # 3 px per mascot pixel here

    low, high, y = app.mascot_pen(MAIN_BAR, x, width, above=True)

    body_left, body_right = low + 4 * unit, high + 4 * unit + 16 * unit
    assert (body_left, body_right) == (x, x + width)  # from the readout's left edge to its right edge, no further
    assert y + app.MASCOT_CANVAS[1] * unit == top == 1528  # feet on the taskbar's top edge


def test_mascot_place_is_a_saved_setting(tmp_path):
    config = app.Config(path=tmp_path / "config.json")
    assert config.mascot_above

    config.mascot_above = False
    config.save()

    assert app.Config.load(config.path).mascot_above is False


def test_mascot_moves_to_the_right_of_a_readout_dragged_to_the_left_end():
    low, high, _ = app.mascot_pen(SECOND_BAR, 2560, 68, above=False)

    assert low == 2560 + 68 + 4 - 8 and high > low


def test_mascot_runs_about_more_when_panicking_and_not_at_all_when_dead():
    def distance(mood):
        _, xs = walk(mood, seconds=60)
        return sum(abs(b - a) for a, b in zip(xs, xs[1:]))

    assert distance("dead") == 0 == distance("confused")
    assert distance("sleepy") < distance("calm") < distance("nervous") < distance("stressed") < distance("panic")


def test_mascot_reacts_at_once_when_the_usage_band_changes():
    pet, _ = walk("sleepy", seconds=1)
    pet.action, pet.until = "rest", 10_000.0  # settled in for a long nap

    app.step_pet(pet, "panic", 2.0, 0.07, random.Random(1), PEN)

    assert (pet.mood, pet.action) == ("panic", "walk")


def test_every_new_percent_above_90_is_announced_once_on_the_way_up():
    announced = {}

    def reading(five, seven=10, stale=False):
        return app.new_alert(usage_with(five=five, seven=seven), stale, announced)

    assert [reading(p) for p in (50, 90, 91, 91, 92, 92.4, 96, 96, 100, 100, 104)] == [0, 0, 91, 0, 92, 0, 96, 0, 100, 0, 0]
    assert reading(3) == 0  # the window reset...
    assert reading(91) == 91  # ...so the next climb is announced again
    assert reading(95, stale=True) == 0  # an old reading announces nothing
    assert reading(95) == 95
    assert reading(95, seven=93) == 93  # each limit counts for itself
    assert app.new_alert(with_models(usage_with(five=95, seven=93), Fable=99), False, announced) == 99


def test_a_limit_just_over_90_is_announced_even_before_91():
    assert app.new_alert(usage_with(five=90.5), False, {}) == 90
    assert app.new_alert(usage_with(five=90), False, {}) == 0


def test_alert_makes_the_mascot_stop_and_jump_three_times_then_carry_on():
    pet = app._Pet(hwnd=0, bar=SECOND_BAR, x=4200.0, alert_left=app.ALERT_FRAMES)
    rng, now, poses = random.Random(5), 0.0, []
    for _ in range(app.ALERT_FRAMES):
        now += 0.07
        poses.append(app.step_pet(pet, "panic", now, 0.07, rng, PEN))

    assert pet.x == 4200.0 and pet.alert_left == 0  # stayed on the spot for the whole announcement
    lifts = [pose.lift for pose in poses]
    assert lifts[0] == 1 and max(lifts) == 4 and lifts[-1] == 0  # leaves the ground at once, lands at the end
    assert sum(1 for a, b in zip(lifts, lifts[1:]) if a == 0 and b > 0) == 2  # two more take-offs: three jumps
    assert all((pose.sign == "!") == (pose.lift > 0) for pose in poses)  # the exclamation mark shows in the air
    assert all(pose.eyes == "wide" for pose in poses)

    after = app.step_pet(pet, "panic", now + 0.07, 0.07, rng, PEN)
    assert after.sign == "" and pet.x != 4200.0  # back to running about


def test_reaching_the_limit_skips_the_alert_and_goes_straight_to_dying():
    pet = app._Pet(hwnd=0, bar=SECOND_BAR, x=4200.0, mood="panic", alert_left=20)

    first = app.step_pet(pet, "dead", 1.0, 0.07, random.Random(1), PEN)

    assert pet.alert_left == 0 and first.sign != "!"
    assert first == app.death_pose(0)


def test_exclamation_mark_is_red_and_fits_above_the_highest_jump():
    top = app.render_mascot(2, app.mascot_pose("panic", "alert", 3, 1))  # frame 3: the top of the jump

    red = rgb(app.STATUS_COLORS["crit"])
    rows = [y for y in range(top.height) for x in range(top.width) if top.getpixel((x, y))[:3] == red and top.getpixel((x, y))[3]]
    assert rows and min(rows) >= 0
    assert max(rows) < (app.MASCOT_ORIGIN[1] - 4) * 2  # above the head, which is 4 mascot pixels up


def test_quit_request_reaches_the_running_copy(monkeypatch):
    import threading

    monkeypatch.setattr(app, "QUIT_EVENT", r"Local\ClaudeUsageTrayTest.quit")  # never the real widget's
    assert app.request_quit() is False  # nothing is listening yet

    heard = []
    listener = threading.Thread(target=lambda: heard.append(app.wait_for_quit_request()))
    listener.start()
    for _ in range(100):  # until the listener has created its event
        if app.request_quit():
            break
        threading.Event().wait(0.02)
    listener.join(3)

    assert heard == [True]


def test_nothing_quicker_than_three_minutes_is_offered():
    # asked once a minute, the usage service starts answering 429 within a few minutes
    assert min(app.INTERVAL_CHOICES) == app.MIN_REFRESH_MINUTES == 3
    assert app.DEFAULT_REFRESH_MINUTES in app.INTERVAL_CHOICES


def test_each_further_refusal_doubles_the_wait_up_to_half_an_hour():
    waits = [app.rate_limit_wait(180, 300, refusals) for refusals in (1, 2, 3, 4, 5)]

    assert waits == [300, 600, 1200, 1800, 1800]
    assert app.rate_limit_wait(3600, 300, 1) == 3600  # never sooner than the normal interval


def test_refresh_is_refused_while_rate_limited(tmp_path):
    poller = app.Poller(app.Config(path=tmp_path / "config.json"), publish=lambda result: None)

    assert poller.refresh_now() is True

    poller._wake.clear()
    poller._refused_until = app.time.monotonic() + 300
    assert poller.refresh_now() is False
    assert not poller._wake.is_set()  # the poller is left asleep: no request goes out


def test_it_sweats_more_above_80_and_more_again_above_90():
    def drops(percent):
        mood = app.mascot_mood(usage_with(five=percent, seven=5), stale=False)
        return app.mascot_pose(mood, "walk", 7, 1).sweat

    assert drops(60) < drops(75) < drops(85) < drops(95)
    assert drops(95) == len(app.SWEAT_PATHS) == len(app.SWEAT_STAGGER) == 6
    image = app.render_mascot(2, app.mascot_pose("panic", "walk", 7, 1))  # all six drops fit in the sprite
    assert image.size == (app.MASCOT_CANVAS[0] * 2, app.MASCOT_CANVAS[1] * 2)


def test_question_mark_shows_for_a_few_seconds_every_few_minutes():
    def sign(frame):
        return app.mascot_pose("confused", "rest", frame, 1).sign

    assert sign(0) == "?" and sign(app.SIGN_FRAMES - 1) == "?"  # as soon as it is confused
    assert sign(app.SIGN_FRAMES) == "" and sign(app.SIGN_EVERY_FRAMES - 1) == ""  # then not for minutes
    assert sign(app.SIGN_EVERY_FRAMES) == "?"
    assert app.SIGN_EVERY_S >= 120 and app.SIGN_FOR_S <= 5


def test_exclamation_jump_comes_at_most_every_few_minutes(tmp_path):
    strip = app.TaskbarStrip(app.Config(path=tmp_path / "config.json"), on_click=None, on_menu=None)

    assert not strip._alert_due(1000.0)  # nothing to announce
    strip._alert = True
    assert strip._alert_due(1000.0)  # the first one goes straight away
    strip._alert = True  # another percent a minute later...
    assert not strip._alert_due(1060.0)
    assert strip._alert  # ...is kept, not lost,
    assert strip._alert_due(1000.0 + app.SIGN_EVERY_S)  # and done once the few minutes are up
    assert not strip._alert_due(1000.0 + app.SIGN_EVERY_S + 1)


def test_at_the_limit_it_dies_and_stays_dead_until_the_reset():
    pet = app._Pet(hwnd=0, bar=SECOND_BAR, x=4200.0, mood="panic")
    rng, poses = random.Random(2), []
    for frame in range(400):
        poses.append(app.step_pet(pet, "dead", frame * 0.07, 0.07, rng, PEN))

    shock, landed = poses[0], poses[app.DEATH_SHOCK + len(app.DEATH_FLIP)]
    assert shock.eyes == "wide" and not shock.upside_down  # a moment of horror first
    assert max(pose.lift for pose in poses) == max(app.DEATH_FLIP)  # thrown into the air
    assert landed.upside_down and landed.eyes == "cross" and landed.lift == 0
    assert any(pose.sign == "soul" for pose in poses[:120])  # its soul floats off...
    assert len(set(poses[120:])) == 1  # ...and after that not a twitch: nothing to repaint
    assert poses[-1] == app.Pose(upside_down=True, eyes="cross")
    assert pet.x == 4200.0

    alive = app.step_pet(pet, "sleepy", 400 * 0.07, 0.07, rng, PEN)  # the limit reset
    assert not alive.upside_down and alive.eyes != "cross"


def test_soul_is_drawn_above_the_body_and_fades_as_it_rises():
    def soul_pixels(phase):
        image = app.render_mascot(2, app.Pose(upside_down=True, eyes="cross", sign="soul", phase=phase))
        return [
            (y, image.getpixel((x, y))[3])
            for y in range(image.height)
            for x in range(image.width)
            if image.getpixel((x, y))[:3] == app.MASCOT_SOUL and image.getpixel((x, y))[3]
        ]

    low, high = soul_pixels(0), soul_pixels(4)
    assert low and high
    assert max(y for y, _ in low) < app.MASCOT_ORIGIN[1] * 2  # clear of the body
    assert min(y for y, _ in high) >= 0 and min(y for y, _ in high) < min(y for y, _ in low)  # higher up, still in frame
    assert high[0][1] < low[0][1]  # and fainter
