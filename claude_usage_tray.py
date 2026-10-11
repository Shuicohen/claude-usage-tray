"""Claude usage tray widget for Windows.

Shows how much of the Claude 5-hour session limit and the 7-day weekly limit is
used: as a tray icon, as a two-line readout beside the clock on every monitor's
taskbar, and as a mascot that paces back and forth on top of that readout and
frets as the limit nears. It reads the sign-in token Claude Code already keeps in
~/.claude/.credentials.json (read-only: the file is never refreshed or
rewritten) and asks the same usage endpoint Claude Code's /usage screen uses,
so no separate API key is needed.

    pythonw claude_usage_tray.py         run in the tray, no console window
    python  claude_usage_tray.py --once  print one reading and exit (troubleshooting)
"""

from __future__ import annotations

import argparse
import ctypes
import functools
import json
import logging
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
import requests
from PIL import Image, ImageDraw, ImageFont, ImageTk

log = logging.getLogger("claude_usage_tray")

APP_NAME = "Claude Usage"
APP_ID = "ClaudeUsageTray"
VERSION = "1.0.0"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = f"claude-usage-tray/{VERSION}"
REQUEST_TIMEOUT_S = 15

# In the home folder, not %APPDATA%: Windows silently redirects %APPDATA% for anything started from a
# Store-packaged app (the Claude desktop app is one), which would give the widget a second, hidden set
# of settings and logs depending on where it was launched from.
DATA_DIR = Path.home() / ".claude-usage-tray"
CONFIG_PATH = DATA_DIR / "config.json"
HISTORY_PATH = DATA_DIR / "history.json"
LOG_PATH = DATA_DIR / "claude_usage_tray.log"

DEFAULT_REFRESH_MINUTES = 3
# Three minutes is the quickest on offer: asked about once a minute, the usage service starts
# refusing (HTTP 429) within a few minutes and then stays touchy for a while.
INTERVAL_CHOICES = (3, 5, 10, 15, 30)
MIN_REFRESH_MINUTES, MAX_REFRESH_MINUTES = 3, 120
QUICK_RETRY_S = 30  # network blips (e.g. right after resume from sleep) retry sooner
QUICK_RETRIES = 3
RATE_LIMIT_BACKOFF_S = 300  # how long to stay away after a first refusal...
RATE_LIMIT_BACKOFF_MAX_S = 1800  # ...doubling with each further one, up to this
MIN_REFRESH_GAP_S = 5  # ignore "Refresh now" spam
WSL_RESCAN_S = 900

WARN_PERCENT = 70
CRIT_PERCENT = 90

PRIMARY_KEYS = ("five_hour", "seven_day")
SHORT_LABELS = {"five_hour": "5h", "seven_day": "7d"}
LIMIT_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "7-day (all models)",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
    "seven_day_oauth_apps": "7-day OAuth apps",
    "seven_day_cowork": "7-day Cowork",
}
LIST_KINDS = {"session": "five_hour", "weekly_all": "seven_day"}  # `limits` list kind -> key above

HISTORY_HOURS = 24
HISTORY_MAX_SAMPLES = 1500  # a little over 24 h at the 1-minute interval
HISTORY_GAP_S = 45 * 60  # a longer silence (PC asleep) breaks the chart line

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TOOLTIP_MAX = 127  # the Windows tray tooltip buffer is 128 characters

ICON_SIZE = 64
SUPERSAMPLE = 4  # draw big, shrink with LANCZOS: tkinter and tray icons have no anti-aliasing
STATUS_COLORS = {"ok": "#0ca30c", "warn": "#fab219", "crit": "#d03b3b"}
ICON_TRACK = (137, 135, 129, 120)  # translucent grey reads on both light and dark taskbars
ICON_STALE = "#898781"

FONT = "Segoe UI"
POPUP_WIDTH = 340
EVENT_POLL_MS = 200
TICK_MS = 30_000  # countdowns in the tooltip and popup are re-rendered this often
DOUBLE_CLICK_S = 0.5

# Taskbar strip. Sizes are in 96-dpi pixels and are scaled per monitor.
STRIP_PAD, STRIP_COLUMN_GAP, STRIP_BAR_WIDTH = 4, 5, 26  # one line is: pad, name, gap, gauge, gap, percent, pad
STRIP_DESIGN_HEIGHT = 44  # three lines need this much; shorter taskbars shrink the strip to fit
STRIP_SYNC_MS = 300  # how often position, visibility and stacking are re-checked
STRIP_GAP_PRIMARY = 4  # space between the strip and the notification area
STRIP_GAP_SECONDARY = 8  # space between the strip and the clock on other monitors
PRIMARY_TRAY_WIDTH = 300  # fallbacks for when Windows does not expose where the
SECONDARY_CLOCK_WIDTH = 84  # notification area or the clock starts (Windows 11 secondary taskbars)
STRIP_DRAG_THRESHOLD = 4
STRIP_INK = {True: ("#ffffff", "#c9c9c9"), False: ("#1a1a1a", "#4d4d4d")}  # dark taskbar?: (number, label)


@dataclass(frozen=True)
class Theme:
    bg: str
    text: str
    secondary: str
    muted: str
    track: str
    grid: str
    border: str
    button: str
    button_hover: str
    series: tuple[str, str]  # history lines: 5-hour, 7-day


DARK = Theme(
    bg="#1a1a19", text="#ffffff", secondary="#c3c2b7", muted="#898781", track="#383835",
    grid="#2c2c2a", border="#383835", button="#2c2c2a", button_hover="#383835",
    series=("#3987e5", "#d95926"),
)
LIGHT = Theme(
    bg="#fcfcfb", text="#0b0b0b", secondary="#52514e", muted="#898781", track="#e1e0d9",
    grid="#e1e0d9", border="#c3c2b7", button="#f0efec", button_hover="#e1e0d9",
    series=("#2a78d6", "#eb6834"),
)


class UsageError(Exception):
    """A failure whose message is short and safe to show in the tooltip and popup."""

    def __init__(self, message: str, retry_after_s: float | None = None, transient: bool = False):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.transient = transient


# ----------------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    access_token: str = field(repr=False)
    expires_at: datetime | None
    plan: str
    source: Path

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def plan_label(oauth: dict) -> str:
    """'max' with tier 'default_claude_max_20x' -> 'Max 20x'."""
    plan = str(oauth.get("subscriptionType") or "").replace("_", " ").title()
    multiplier = re.search(r"_(\d+x)$", str(oauth.get("rateLimitTier") or ""))
    return f"{plan} {multiplier.group(1)}".strip() if multiplier else plan


def read_credentials_file(path: Path) -> Credentials | None:
    """Parse one Claude Code credentials file; None when it is missing or unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    expires_ms = oauth.get("expiresAt")
    expires_at = (
        datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
        if isinstance(expires_ms, (int, float))
        else None
    )
    return Credentials(str(oauth["accessToken"]), expires_at, plan_label(oauth), path)


def windows_credential_paths() -> list[Path]:
    paths = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        paths.append(Path(config_dir) / ".credentials.json")
    paths.append(Path.home() / ".claude" / ".credentials.json")
    return paths


def _wsl_distros() -> list[str]:
    try:
        result = subprocess.run(
            ["wsl.exe", "--list", "--quiet"],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    # wsl.exe writes UTF-16LE unless WSL_UTF8=1 is set
    encoding = "utf-16-le" if b"\x00" in result.stdout else "utf-8"
    names = (line.strip() for line in result.stdout.decode(encoding, errors="ignore").splitlines())
    return [name for name in names if name and not name.startswith("docker-desktop")]


_wsl_scan: tuple[float, list[Path]] | None = None


def wsl_credential_paths() -> list[Path]:
    r"""Credentials files inside WSL distros, reached through \\wsl.localhost.

    Touching that share starts the distro, so the scan is cached and is only
    asked for when the Windows side has no usable token.
    """
    global _wsl_scan
    if _wsl_scan and time.monotonic() - _wsl_scan[0] < WSL_RESCAN_S:
        return _wsl_scan[1]
    paths = []
    for distro in _wsl_distros():
        root = Path(f"\\\\wsl.localhost\\{distro}")
        homes = [root / "root"]
        try:
            homes += list((root / "home").iterdir())
        except OSError:
            continue
        for home in homes:
            candidate = home / ".claude" / ".credentials.json"
            try:
                if candidate.is_file():
                    paths.append(candidate)
            except OSError:
                continue
    _wsl_scan = (time.monotonic(), paths)
    return paths


def load_credentials(now: datetime | None = None) -> Credentials:
    """The first usable Claude Code token: Windows locations first, WSL as the fallback."""
    now = now or datetime.now(timezone.utc)
    found = [c for path in windows_credential_paths() if (c := read_credentials_file(path))]
    if all(c.is_expired(now) for c in found):
        found += [c for path in wsl_credential_paths() if (c := read_credentials_file(path))]
    if not found:
        raise UsageError("Not signed in - run `claude` once to sign in to Claude Code")
    usable = [c for c in found if not c.is_expired(now)]
    if not usable:
        raise UsageError("Sign-in token expired - it renews the next time Claude Code runs")
    return usable[0]


# ----------------------------------------------------------------------------
# Usage data
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    key: str
    label: str
    percent: float
    resets_at: datetime | None


@dataclass(frozen=True)
class Usage:
    fetched_at: datetime
    limits: tuple[Limit, ...]
    plan: str = ""

    def get(self, key: str) -> Limit | None:
        return next((limit for limit in self.limits if limit.key == key), None)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scope_name(scope: object) -> str:
    """The model or surface a scoped limit applies to, e.g. 'Opus'."""
    if not isinstance(scope, dict):
        return ""
    for part in (scope.get("model"), scope.get("surface")):
        name = part.get("display_name") if isinstance(part, dict) else part
        if isinstance(name, str) and name:
            return name
    return ""


def _listed_limit(entry: dict) -> Limit:
    """One entry of the reply's `limits` list."""
    kind = str(entry.get("kind") or "limit")
    name = _scope_name(entry.get("scope"))
    if kind in LIST_KINDS:
        key = LIST_KINDS[kind]
        label = LIMIT_LABELS[key]
    elif kind == "weekly_scoped":
        key, label = f"{kind}:{name}", f"7-day {name}" if name else "7-day (scoped)"
    else:
        key, label = f"{kind}:{name}", f"{kind.replace('_', ' ').capitalize()} {name}".strip()
    return Limit(key, label, float(entry["percent"]), _parse_time(entry.get("resets_at")))


def parse_usage(payload: dict, fetched_at: datetime, plan: str = "") -> Usage:
    """Turn the usage endpoint's JSON into limits, 5-hour and 7-day first.

    The reply describes the same windows twice: a `limits` list, which is the
    only place that names per-model weekly limits, and older top-level objects
    (`five_hour`, `seven_day`, ...). The list wins when it is there; the
    top-level objects fill in whatever it lacks. Top-level keys this code does
    not know are skipped - the reply is full of internal codenames.
    """
    found: dict[str, Limit] = {}
    listed = payload.get("limits")
    for entry in listed if isinstance(listed, list) else ():
        if isinstance(entry, dict) and _is_number(entry.get("percent")):
            limit = _listed_limit(entry)
            found.setdefault(limit.key, limit)
    list_missing = not found
    for key, label in LIMIT_LABELS.items():
        window = payload.get(key)
        if not (key in PRIMARY_KEYS or list_missing):
            continue  # the list already covers the per-model limits; adding these would duplicate them
        if isinstance(window, dict) and _is_number(window.get("utilization")):
            found.setdefault(key, Limit(key, label, float(window["utilization"]), _parse_time(window.get("resets_at"))))
    limits = sorted(
        found.values(),
        key=lambda limit: PRIMARY_KEYS.index(limit.key) if limit.key in PRIMARY_KEYS else len(PRIMARY_KEYS),
    )
    return Usage(fetched_at, tuple(limits), plan)


def _retry_after(response: requests.Response) -> float:
    try:
        return max(float(response.headers.get("Retry-After", "")), RATE_LIMIT_BACKOFF_S)
    except ValueError:
        return RATE_LIMIT_BACKOFF_S


def rate_limit_wait(interval_s: float, retry_after_s: float, refusals: int) -> float:
    """Seconds to stay away once the service has refused `refusals` times in a row: twice as long each time."""
    return max(interval_s, min(retry_after_s * 2 ** (refusals - 1), RATE_LIMIT_BACKOFF_MAX_S))


def request_usage(creds: Credentials) -> dict:
    """GET the raw usage payload, mapping every failure to a UsageError."""
    try:
        response = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "anthropic-beta": OAUTH_BETA,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=False,  # the token goes to this one host and nowhere else
        )
    except requests.RequestException as exc:
        raise UsageError(f"Network error ({type(exc).__name__})", transient=True) from exc
    if response.status_code == 401:
        raise UsageError("Sign-in token rejected - open Claude Code to renew it")
    if response.status_code == 429:
        log.warning("Usage API refused with 429; Retry-After header: %r", response.headers.get("Retry-After"))
        raise UsageError(
            "Rate limited by Anthropic (asked too often) - waiting before trying again",
            retry_after_s=_retry_after(response),
        )
    if response.status_code >= 300:
        raise UsageError(
            f"Usage API returned HTTP {response.status_code}", transient=response.status_code >= 500
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise UsageError("Usage API sent an unreadable reply") from exc
    if not isinstance(payload, dict):
        raise UsageError("Usage API sent an unexpected reply")
    return payload


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------


def severity(percent: float) -> str:
    """'ok' below 70 %, 'warn' from 70 % to 90 %, 'crit' above 90 %."""
    if percent > CRIT_PERCENT:
        return "crit"
    if percent >= WARN_PERCENT:
        return "warn"
    return "ok"


def format_countdown(target: datetime | None, now: datetime) -> str:
    """'45m', '2h 05m', '3d 4h'; 'now' once the moment has passed; '' when unknown."""
    if target is None:
        return ""
    seconds = (target - now).total_seconds()
    if seconds <= 0:
        return "now"
    minutes = int(-(-seconds // 60))  # round up so the last half minute still reads "1m"
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def uses_12_hour_clock() -> bool:
    """Follow the Windows short-time format (Settings > Time & language > Region)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as key:
            return "H" not in winreg.QueryValueEx(key, "sShortTime")[0]
    except OSError:
        return False


def format_clock(moment: datetime, twelve_hour: bool) -> str:
    local = moment.astimezone()
    return local.strftime("%I:%M %p").lstrip("0") if twelve_hour else local.strftime("%H:%M")


def format_reset_clock(resets_at: datetime, now: datetime, twelve_hour: bool) -> str:
    """'today 15:45', 'tomorrow 09:00', 'Wed 09:00'."""
    local = (resets_at + timedelta(seconds=30)).astimezone()  # the API reports hh:59:59.7 for a reset on the hour
    days = (local.date() - now.astimezone().date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    else:
        day = local.strftime("%a" if days < 7 else "%a %d %b")
    return f"{day} {format_clock(local, twelve_hour)}"


def describe_reset(limit: Limit, now: datetime, twelve_hour: bool) -> str:
    if limit.resets_at is None:
        return "No active window"
    if limit.resets_at <= now:
        return "Reset time passed - waiting for fresh data"
    countdown = format_countdown(limit.resets_at, now)
    return f"Resets in {countdown} ({format_reset_clock(limit.resets_at, now, twelve_hour)})"


def build_tooltip(usage: Usage | None, error: str | None, now: datetime) -> str:
    lines = [f"{APP_NAME} ({usage.plan})" if usage and usage.plan else APP_NAME]
    limits = [limit for key in PRIMARY_KEYS if usage and (limit := usage.get(key))]
    if error and limits:
        # the reset countdowns give way to the reason, which would not fit otherwise
        readings = " | ".join(f"{SHORT_LABELS[limit.key]}: {limit.percent:.0f}%" for limit in limits)
        lines += [f"{readings} (last reading)", error]
    elif error:
        lines.append(f"Error: {error}")
    elif usage is None:
        lines.append("Loading...")
    else:
        for label, limit in readout_rows(usage):
            if limit is None:
                continue
            line = f"{label}: {limit.percent:.0f}%"
            if limit.resets_at and limit.resets_at > now:
                line += f" - resets in {format_countdown(limit.resets_at, now)}"
            lines.append(line)
    return "\n".join(lines)[:TOOLTIP_MAX]


# ----------------------------------------------------------------------------
# Settings, history, startup
# ----------------------------------------------------------------------------


@dataclass
class Config:
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    taskbar_strip: bool = True
    mascot: bool = True
    mascot_above: bool = True  # on top of the taskbar over the readout; False: inside it, to the readout's left
    # Where the strip was dragged to: 96-dpi pixels between it and the clock. None = the default spot.
    strip_gap_primary: float | None = None
    strip_gap_secondary: float | None = None
    path: Path = field(default=CONFIG_PATH, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    STORED = ("refresh_minutes", "taskbar_strip", "mascot", "mascot_above", "strip_gap_primary", "strip_gap_secondary")

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> Config:
        config = cls(path=path)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return config  # first run or an unreadable file: keep the defaults
        if not isinstance(stored, dict):
            return config
        if _is_number(stored.get("refresh_minutes")):
            config.refresh_minutes = min(max(int(stored["refresh_minutes"]), MIN_REFRESH_MINUTES), MAX_REFRESH_MINUTES)
        for name in ("taskbar_strip", "mascot", "mascot_above"):
            if isinstance(stored.get(name), bool):
                setattr(config, name, stored[name])
        for name in ("strip_gap_primary", "strip_gap_secondary"):
            if _is_number(stored.get(name)):
                setattr(config, name, float(stored[name]))
        return config

    def save(self) -> None:
        stored = {name: getattr(self, name) for name in self.STORED}
        with self._lock:  # the tray menu and the taskbar strip save from different threads
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(stored, indent=2), encoding="utf-8")


class History:
    """Recent (unix time, 5-hour %, 7-day %) samples, kept on disk so the chart survives restarts."""

    def __init__(self, path: Path = HISTORY_PATH):
        self.path = path
        self.samples: deque[tuple[float, float | None, float | None]] = deque(maxlen=HISTORY_MAX_SAMPLES)
        try:
            for stamp, five, seven in json.loads(path.read_text(encoding="utf-8")):
                self.samples.append((float(stamp), five, seven))
        except (OSError, ValueError, TypeError):
            pass  # no history yet, or an unreadable file: start fresh

    def add(self, usage: Usage) -> None:
        five, seven = (usage.get(key) for key in PRIMARY_KEYS)
        self.samples.append(
            (usage.fetched_at.timestamp(), five.percent if five else None, seven.percent if seven else None)
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.path.with_suffix(".tmp")
            scratch.write_text(json.dumps(list(self.samples)), encoding="utf-8")
            os.replace(scratch, self.path)
        except OSError:
            log.warning("Could not save history", exc_info=True)

    def since(self, start: datetime) -> list[tuple[float, float | None, float | None]]:
        cutoff = start.timestamp()
        return [sample for sample in self.samples if sample[0] >= cutoff]


def startup_command() -> str:
    """The command line that relaunches this app at sign-in without a console window."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    windowless = Path(sys.executable).with_name("pythonw.exe")
    interpreter = windowless if windowless.exists() else Path(sys.executable)
    return f'"{interpreter}" "{Path(__file__).resolve()}"'


def is_startup_enabled(key_path: str = RUN_KEY, name: str = APP_ID) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, name)
        return True
    except OSError:
        return False


def set_startup(enabled: bool, key_path: str = RUN_KEY, name: str = APP_ID) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, startup_command())
            return
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------------
# Drawing
# ----------------------------------------------------------------------------


def render_icon(usage: Usage | None, stale: bool = False) -> Image.Image:
    """Tray icon: two vertical gauges, 5-hour on the left and 7-day on the right."""
    size = ICON_SIZE * SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad, gap, radius = size * 6 // 64, size * 8 // 64, size * 6 // 64
    width = (size - 2 * pad - gap) // 2
    for index, key in enumerate(PRIMARY_KEYS):
        left = pad + index * (width + gap)
        top, bottom = pad, size - pad - 1
        draw.rounded_rectangle((left, top, left + width - 1, bottom), radius, fill=ICON_TRACK)
        limit = usage.get(key) if usage else None
        if limit is None or limit.percent <= 0:
            continue
        # never shorter than a pill, so 1 % is still visible at 16 px
        height = max(round((bottom - top) * min(limit.percent, 100) / 100), 2 * radius)
        color = ICON_STALE if stale else STATUS_COLORS[severity(limit.percent)]
        draw.rounded_rectangle((left, bottom - height, left + width - 1, bottom), radius, fill=color)
    return image.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)


def bar_image(width: int, height: int, percent: float, fill: str, track: str, background: str) -> Image.Image:
    """A horizontal progress bar with rounded ends."""
    s = SUPERSAMPLE
    image = Image.new("RGB", (width * s, height * s), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width * s - 1, height * s - 1), height * s // 2, fill=track)
    if percent > 0:
        filled = max(round(width * s * min(percent, 100) / 100), height * s)
        draw.rounded_rectangle((0, 0, filled - 1, height * s - 1), height * s // 2, fill=fill)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _runs(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Split time-ordered points wherever the gap is too long to draw a line across."""
    runs: list[list[tuple[float, float]]] = []
    for point in points:
        if runs and point[0] - runs[-1][-1][0] <= HISTORY_GAP_S:
            runs[-1].append(point)
        else:
            runs.append([point])
    return runs


def history_image(
    width: int,
    height: int,
    samples: list[tuple[float, float | None, float | None]],
    start: float,
    end: float,
    theme: Theme,
) -> Image.Image:
    """Line chart of the 5-hour and 7-day percentages over [start, end] on one 0-100 % axis."""
    s = SUPERSAMPLE
    w, h, inset = width * s, height * s, 2 * s

    def x(stamp: float) -> float:
        return (w - 1) * (stamp - start) / (end - start)

    def y(percent: float) -> float:
        return inset + (h - 2 * inset - 1) * (1 - min(max(percent, 0), 100) / 100)

    image = Image.new("RGB", (w, h), theme.bg)
    draw = ImageDraw.Draw(image)
    for percent in (100, 50, 0):
        draw.line((0, y(percent), w, y(percent)), fill=theme.track if percent == 0 else theme.grid, width=s)
    for column, color in zip((1, 2), theme.series):
        series = [(sample[0], sample[column]) for sample in samples if sample[column] is not None]
        for run in _runs(series):
            points = [(x(stamp), y(percent)) for stamp, percent in run]
            if len(points) == 1:
                (cx, cy), r = points[0], 1.5 * s
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
            else:
                draw.line(points, fill=color, width=2 * s, joint="curve")
    return image.resize((width, height), Image.Resampling.LANCZOS)


_fonts: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """Segoe UI from the Windows fonts folder, for text drawn outside tkinter."""
    if (bold, size) not in _fonts:
        _fonts[bold, size] = ImageFont.truetype("seguisb.ttf" if bold else "segoeui.ttf", size)
    return _fonts[bold, size]


def strip_label(limit: Limit) -> str:
    """The few characters that name a limit on the taskbar: '5h', '7d', 'Fable'."""
    if limit.key in SHORT_LABELS:
        return SHORT_LABELS[limit.key]
    return limit.label.removeprefix("7-day ").strip("()")[:6]


def readout_rows(usage: Usage | None) -> list[tuple[str, Limit | None]]:
    """What the taskbar readout shows, top to bottom: both main limits, then the fullest per-model limit.

    A taskbar is three short lines tall at most, so a plan with several
    per-model limits shows only the one closest to running out.
    """
    rows = [(SHORT_LABELS[key], usage.get(key) if usage else None) for key in PRIMARY_KEYS]
    extras = [limit for limit in usage.limits if limit.key not in PRIMARY_KEYS] if usage else []
    if extras:
        fullest = max(extras, key=lambda limit: limit.percent)
        rows.append((strip_label(fullest), fullest))
    return rows


def _strip_font_size(rows: list) -> int:
    return 11 if len(rows) > 2 else 12  # three lines only fit a taskbar in slightly smaller type


def _percent_text(limit: Limit | None) -> str:
    return f"{limit.percent:.0f}%" if limit else "--"


def _strip_columns(rows: list, font_px: int) -> tuple[int, int]:
    """Widths of the name column and the percentage column, so that every line's gauge starts and ends alike.

    The percentage column allows for two digits even when showing one, so the
    readout does not twitch as a number passes 10 %; only a 100 % widens it.
    """
    label_font, number_font = _font(False, font_px), _font(True, font_px)
    names = max(label_font.getlength(label) for label, _ in rows)
    numbers = max(number_font.getlength(text) for text in ["00%", *(_percent_text(limit) for _, limit in rows)])
    return math.ceil(names), math.ceil(numbers)


def strip_design_width(usage: Usage | None) -> int:
    """How wide the readout is at 100 % scaling: as wide as its longest name and number need."""
    rows = readout_rows(usage)
    names, numbers = _strip_columns(rows, _strip_font_size(rows))
    return 2 * STRIP_PAD + names + numbers + 2 * STRIP_COLUMN_GAP + STRIP_BAR_WIDTH


def render_strip(width: int, height: int, unit: float, usage: Usage | None, stale: bool, dark: bool) -> Image.Image:
    """Taskbar readout: one line per limit, reading name - gauge - percentage.

    `unit` is screen pixels per design pixel. The image is never fully
    transparent: alpha 1 cannot be seen, but Windows hands clicks on alpha-0
    pixels to whatever is underneath, which would make the gaps unclickable.
    """
    number_ink, label_ink = STRIP_INK[dark]
    rows = readout_rows(usage)
    font_px = max(round(_strip_font_size(rows) * unit), 8)
    label_font, number_font = _font(False, font_px), _font(True, font_px)
    pad, gap, bar_height = round(STRIP_PAD * unit), round(STRIP_COLUMN_GAP * unit), max(round(4 * unit), 2)
    pitch = min(height / len(rows), 16 * unit)  # three lines fill the taskbar; two sit where the clock's two do
    first_middle = (height - pitch * len(rows)) / 2 + pitch / 2
    names, numbers = _strip_columns(rows, font_px)
    bar_left, bar_right = pad + names + gap, width - pad - numbers - gap

    s = SUPERSAMPLE
    gauges = Image.new("RGBA", (width * s, height * s), (0, 0, 0, 0))
    gauge_draw = ImageDraw.Draw(gauges)
    label_mask, number_mask = Image.new("L", (width, height), 0), Image.new("L", (width, height), 0)
    for index, (label, limit) in enumerate(rows):
        middle = first_middle + index * pitch
        ImageDraw.Draw(label_mask).text((pad, middle), label, fill=255, font=label_font, anchor="lm")
        # an out-of-date number is drawn in the quieter label ink
        ImageDraw.Draw(label_mask if stale or limit is None else number_mask).text(
            (width - pad, middle), _percent_text(limit), fill=255, font=number_font, anchor="rm"
        )
        if bar_right - bar_left < bar_height:
            continue  # no room for a gauge: the numbers alone still tell the story
        top = round(middle - bar_height / 2) * s
        left, right, bottom, radius = bar_left * s, bar_right * s - 1, top + bar_height * s - 1, bar_height * s // 2
        gauge_draw.rounded_rectangle((left, top, right, bottom), radius, fill=ICON_TRACK)
        if limit and limit.percent > 0:
            filled = max(round((right - left) * min(limit.percent, 100) / 100), bar_height * s)
            color = ICON_STALE if stale else STATUS_COLORS[severity(limit.percent)]
            gauge_draw.rounded_rectangle((left, top, left + filled, bottom), radius, fill=color)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 1))
    image.alpha_composite(gauges.resize((width, height), Image.Resampling.LANCZOS))
    for mask, ink in ((label_mask, label_ink), (number_mask, number_ink)):
        text = Image.new("RGBA", (width, height), ink)
        text.putalpha(mask)
        image.alpha_composite(text)
    return image


# ----------------------------------------------------------------------------
# Mascot
# ----------------------------------------------------------------------------
# The Claude logo is a 16 x 10 pixel creature: a 12 x 8 body, an arm two pixels long on each side,
# two eyes and four legs. Here it walks along the top edge of the taskbar, back and forth over
# the readout (or, as a setting, stands in the taskbar beside it), and gets more and more worked
# up as the usage climbs. Everything below is drawn in "mascot pixels" of that grid.

MASCOT_UNIT = 2  # 96-dpi pixels per mascot pixel
MASCOT_CANVAS = (24, 21)  # the logo plus air around it for sweat, snoring, signs and jumping
MASCOT_ORIGIN = (4, 11)  # where the logo's top-left corner sits in that canvas
MASCOT_FRAME_MS = 70
MASCOT_PEN_MIN, MASCOT_PEN_MAX = 36, 160  # how much taskbar it may pace, in 96-dpi pixels
MASCOT_GAP = 4  # 96-dpi pixels kept clear between it and the readout
MASCOT_FEET = 4  # 96-dpi pixels between its feet and the bottom of the taskbar
MASCOT_BODY, MASCOT_EYE, MASCOT_SWEAT = "#d97757", "#262624", "#4aa8ee"
SWEAT_PATHS = (  # the flight of each drop, relative to the logo's top-left corner
    ((13, -1), (14, -2), (15, -3), (16, -3), (17, -2), (18, -1)),
    ((2, -1), (1, -2), (0, -3), (-1, -3), (-2, -2), (-3, -1)),
    ((9, -1), (9, -2), (10, -3), (10, -4), (11, -4), (11, -3)),
    ((6, -1), (5, -2), (5, -3), (4, -4), (4, -4), (3, -3)),
    ((12, -1), (13, -3), (14, -5), (15, -5), (16, -4), (17, -3)),
    ((3, -1), (2, -3), (1, -5), (0, -5), (-1, -4), (-2, -3)),
)
SWEAT_STAGGER = (0, 3, 1, 4, 2, 5)  # each drop takes off a little after the one before
SWEAT_PAUSE = {1: 8, 2: 2}  # frames between flights: a lone drop is occasional, a drenching is constant
GLYPHS = {
    "z": ("####", "..#.", ".#..", "####"),
    "?": ("###", "#.#", "..#", ".#.", "...", ".#."),
    "!": ("##", "##", "##", "..", "##"),
    "soul": (".###.", "#####", "#.#.#", "#####", "#####", "#.#.#"),
}
MASCOT_SOUL = (176, 186, 206)
SIGN_EVERY_S = 180  # the "!" jump and the "?" come at most this often, so they nag rather than pester
SIGN_FOR_S = 4  # how long the "?" stays up each time
SIGN_EVERY_FRAMES, SIGN_FRAMES = SIGN_EVERY_S * 1000 // MASCOT_FRAME_MS, SIGN_FOR_S * 1000 // MASCOT_FRAME_MS
DEATH_SHOCK = 8  # frames frozen in horror when the limit is hit...
DEATH_FLIP = (2, 4, 5, 5, 4, 2, 0, 1, 0)  # ...then thrown into the air and onto its back, with a bounce
SOUL_FRAMES = (6, 42)  # after landing: the frames between which its soul floats away
ALERT_HOP = (1, 2, 3, 4, 4, 3, 2, 1, 0, 0, 0, 0)  # how high it is on each frame of one jump, then a breather
ALERT_FRAMES = 3 * len(ALERT_HOP)  # three jumps, about two and a half seconds


@dataclass(frozen=True)
class Pose:
    legs: int = 0  # 0 standing, 1 and 2 the two halves of a step, 3 tucked under (sitting)
    eyes: str = "open"  # open, shut, wide, cross
    look: int = 0  # -1 left, 1 right
    arms: str = "down"  # down, up, left_up, right_up
    mouth: bool = False
    lift: int = 0  # mascot pixels off the ground
    upside_down: bool = False
    sweat: int = 0  # drops in the air, up to len(SWEAT_PATHS)
    phase: int = 0  # how far along the drops or the z's are
    sign: str = ""  # "z" asleep, "?" no idea


@dataclass(frozen=True)
class Mood:
    speed: float = 0  # 96-dpi pixels per second; 0 stays put
    step_frames: int = 4  # frames per half step: smaller is a quicker scuttle
    walk_s: tuple[float, float] = (0, 0)  # how long it walks before stopping
    rest_s: tuple[float, float] = (0, 0)  # how long it stops; (0, 0) never stops
    sweat: int = 0
    naps: bool = False
    frantic: bool = False


MASCOT_STRESSED_PERCENT = 80
MOODS = {  # by the fullest limit on the readout
    "sleepy": Mood(speed=8, step_frames=6, walk_s=(2, 5), rest_s=(10, 25), naps=True),  # under 25 %
    "calm": Mood(speed=12, step_frames=4, walk_s=(3, 8), rest_s=(4, 10)),  # 25-50 %
    "busy": Mood(speed=20, step_frames=3, walk_s=(4, 10), rest_s=(1.5, 4), sweat=1),  # 50-70 %
    "nervous": Mood(speed=32, step_frames=2, walk_s=(2, 5), rest_s=(0.5, 1.5), sweat=2),  # 70-80 %
    "stressed": Mood(speed=44, step_frames=2, walk_s=(2, 4), rest_s=(0.4, 1.0), sweat=4),  # 80-90 %
    "panic": Mood(speed=70, step_frames=1, walk_s=(0.6, 2.5), sweat=6, frantic=True),  # over 90 %
    "dead": Mood(),  # 100 %: the limit is reached, until it resets
    "confused": Mood(),  # no reading, or an out-of-date one
}


def mascot_mood(usage: Usage | None, stale: bool) -> str:
    """How worked up the mascot is: it follows the fullest limit on the readout."""
    limits = [limit for _, limit in readout_rows(usage) if limit]
    if stale or not limits:
        return "confused"
    worst = max(limit.percent for limit in limits)
    if worst >= 100:
        return "dead"
    if worst > CRIT_PERCENT:
        return "panic"
    if worst >= MASCOT_STRESSED_PERCENT:
        return "stressed"
    if worst >= WARN_PERCENT:
        return "nervous"
    return "busy" if worst >= 50 else "calm" if worst >= 25 else "sleepy"


def new_alert(usage: Usage | None, stale: bool, announced: dict[str, int]) -> int:
    """The percent to announce, or 0: a limit on the readout has reached a whole percent above 90 it had not before.

    So 91, 92, ... 100 are each announced once on the way up. `announced`
    holds the last percent announced per limit and is updated here; a limit
    that falls back (its window reset) starts over.
    """
    newest = 0
    for _, limit in readout_rows(None if stale else usage):
        if limit is None:
            continue
        level = min(math.floor(limit.percent), 100) if limit.percent > CRIT_PERCENT else 0
        if level > announced.get(limit.key, 0):
            newest = max(newest, level)
        announced[limit.key] = level
    return newest


def death_pose(frame: int) -> Pose:
    """The limit is reached: a moment of horror, a flip onto its back, its soul leaves, then perfectly still."""
    if frame < DEATH_SHOCK:
        return Pose(eyes="wide", arms="up", mouth=True, sweat=len(SWEAT_PATHS), phase=frame % len(SWEAT_PATHS[0]))
    flip = frame - DEATH_SHOCK
    if flip < len(DEATH_FLIP):
        over = flip >= len(DEATH_FLIP) // 2
        return Pose(eyes="cross" if over else "wide", arms="up", mouth=not over, lift=DEATH_FLIP[flip], upside_down=over)
    still = flip - len(DEATH_FLIP)
    first, last = SOUL_FRAMES
    if still >= last:
        return Pose(upside_down=True, eyes="cross")  # and so it stays, without another repaint, until the reset
    legs = 1 if 3 <= still < 6 else 2 if 10 <= still < 13 else 0  # two last twitches
    if still < first:
        return Pose(upside_down=True, eyes="cross", legs=legs)
    return Pose(upside_down=True, eyes="cross", legs=legs, sign="soul", phase=(still - first) * 5 // (last - first))


def mascot_pose(mood_name: str, action: str, frame: int, direction: int) -> Pose:
    """What the mascot looks like on a given animation frame. `action` is "walk", "rest" or "alert".

    For "dead" and "confused", which tell a story from their first frame,
    `frame` counts from the moment that mood began.
    """
    if mood_name == "dead":
        return death_pose(frame)
    if action == "alert":  # announcing a new percent: high jumps on the spot, arms up, under an exclamation mark
        hop = ALERT_HOP[frame % len(ALERT_HOP)]
        return Pose(eyes="wide", arms="up" if hop else "down", mouth=True, lift=hop, sign="!" if hop else "")
    mood = MOODS[mood_name]
    eyes = "shut" if frame % 48 < 2 else "open"  # a blink every few seconds
    # where the sweat is along its flight; wraps round, so the same few poses (and cached sprites) repeat
    flight = len(SWEAT_PATHS[0]) + SWEAT_PAUSE.get(mood.sweat, 0)
    phase = (frame if mood.frantic else frame // 2) % flight
    if mood_name == "confused":  # looks left and right for an answer; the question mark only now and then
        asking = frame % SIGN_EVERY_FRAMES < SIGN_FRAMES
        return Pose(eyes=eyes, look=(-1, 0, 1, 0)[(frame // 30) % 4], sign="?" if asking else "")
    legs = 1 + (frame // mood.step_frames) % 2
    if mood.frantic:  # arms flailing, bouncing off the ground, mouth open
        arms = ("left_up", "right_up")[(frame // 2) % 2]
        return Pose(legs, "wide", direction, arms, mouth=True, lift=(frame // 2) % 2, sweat=mood.sweat, phase=phase)
    if action == "walk":
        return Pose(legs, eyes, direction, sweat=mood.sweat, phase=phase)
    if mood.naps:
        return Pose(legs=3, eyes="shut", sign="z", phase=(frame // 10) % 4)
    if mood_name in ("nervous", "stressed"):  # eyes darting about
        return Pose(look=(-1, 1)[(frame // 5) % 2], sweat=mood.sweat, phase=phase)
    waving = frame % 90 < 12 and (frame % 90) // 3 % 2 == 0  # a little wave every six seconds
    return Pose(eyes=eyes, arms="right_up" if waving else "down", sweat=mood.sweat, phase=phase)


def mascot_face(mood_name: str) -> Pose:
    """A still portrait for the details window."""
    faces = {
        "dead": Pose(upside_down=True, eyes="cross"),
        "panic": Pose(eyes="wide", mouth=True, arms="up"),
        "stressed": Pose(eyes="wide"),
        "nervous": Pose(look=1),
        "sleepy": Pose(eyes="shut"),
    }
    return faces.get(mood_name, Pose())


@functools.lru_cache(maxsize=512)
def render_mascot(unit: int, pose: Pose) -> Image.Image:
    """One animation frame, `unit` screen pixels per mascot pixel. Cached: do not modify the result."""
    columns, rows = MASCOT_CANVAS
    origin_x, origin_y = MASCOT_ORIGIN
    image = Image.new("RGBA", (columns * unit, rows * unit), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    sitting = pose.legs == 3
    lift = pose.lift - (2 if sitting else 0)  # sitting down puts the body where the legs were

    def box(left: float, top: float, width: float, height: float, body_part: bool) -> tuple[int, int, int, int]:
        if body_part:
            if pose.upside_down:
                top = 10 - top - height
            top -= lift
        x, y = round((origin_x + left) * unit), round((origin_y + top) * unit)
        return x, y, x + round(width * unit) - 1, y + round(height * unit) - 1

    def fill(left: float, top: float, width: float, height: float, color: str, body_part: bool = True) -> None:
        draw.rectangle(box(left, top, width, height, body_part), fill=color)

    fill(2, 0, 12, 8, MASCOT_BODY)
    for side, column in (("left", 0), ("right", 14)):
        fill(column, 1 if pose.arms in ("up", f"{side}_up") else 4, 2, 2, MASCOT_BODY)
    for index, column in enumerate(() if sitting else (3, 5, 10, 12)):
        mid_step = pose.legs and index % 2 == pose.legs - 1  # legs 1 lifts the 1st and 3rd, legs 2 the others
        fill(column, 8, 1, 1 if mid_step else 2, MASCOT_BODY)
    for column in (4, 11):
        column += pose.look
        if pose.eyes == "open":
            fill(column, 2, 1, 2, MASCOT_EYE)
        elif pose.eyes == "shut":
            fill(column, 3, 1, 1, MASCOT_EYE)
        elif pose.eyes == "wide":
            fill(column - (column > 7), 2, 2, 2, MASCOT_EYE)  # each eye grows toward the middle
        else:  # "cross": an X where each eye was
            x0, y0, x1, y1 = box(column - 0.5, 2, 2, 2, True)
            draw.line((x0, y0, x1, y1), fill=MASCOT_EYE, width=max(unit // 2, 1))
            draw.line((x0, y1, x1, y0), fill=MASCOT_EYE, width=max(unit // 2, 1))
    if pose.mouth:
        fill(7, 5, 2, 1, MASCOT_EYE)

    for index, path in enumerate(SWEAT_PATHS[: pose.sweat]):
        step = (pose.phase + SWEAT_STAGGER[index]) % (len(path) + SWEAT_PAUSE.get(pose.sweat, 0))
        if step < len(path):
            fill(*path[step], 1, 1, MASCOT_SWEAT, body_part=False)
    ink = MASCOT_BODY
    if pose.sign == "z":  # z's drift up and away: none, one, two, then only the far one
        spots = [spot for spot, on in (((12, -4), pose.phase in (1, 2)), ((16, -7), pose.phase in (2, 3))) if on]
    elif pose.sign == "soul":  # rises from the body, swaying, and thins out as it goes
        spots, ink = [(5 + pose.phase % 2, -7 - pose.phase)], (*MASCOT_SOUL, 255 - 45 * pose.phase)
    elif pose.sign:  # "?" and "!" float just above its head, wherever the head is
        spots = [(7, -len(GLYPHS[pose.sign]) - 1 - max(lift, 0))]
        ink = STATUS_COLORS["crit"] if pose.sign == "!" else MASCOT_BODY
    else:
        spots = []
    for left, top in spots:
        for row, line in enumerate(GLYPHS[pose.sign]):
            for column, mark in enumerate(line):
                if mark == "#":
                    fill(left + column, top + row, 1, 1, ink, body_part=False)
    return image


# ----------------------------------------------------------------------------
# Windows glue
# ----------------------------------------------------------------------------

# Private library handles: prototypes set on ctypes.windll.* would be shared with pystray.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shcore = ctypes.WinDLL("shcore")
_dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


def _declare(function, restype, *argtypes) -> None:
    """Spell out a prototype: ctypes otherwise assumes 32-bit ints, which truncates 64-bit handles."""
    function.restype = restype
    function.argtypes = argtypes


_HWND, _UINT, _DWORD, _INT, _BOOL, _HDC = (
    wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_int, wintypes.BOOL, wintypes.HDC,
)
_POINT_P = ctypes.POINTER(wintypes.POINT)
_declare(_user32.FindWindowExW, _HWND, _HWND, _HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_declare(_user32.GetWindowRect, _BOOL, _HWND, ctypes.POINTER(wintypes.RECT))
_declare(_user32.IsWindowVisible, _BOOL, _HWND)
_declare(_user32.WindowFromPoint, _HWND, wintypes.POINT)
_declare(_user32.GetAncestor, _HWND, _HWND, _UINT)
_declare(_user32.GetWindow, _HWND, _HWND, _UINT)
_declare(_user32.MonitorFromWindow, wintypes.HMONITOR, _HWND, _DWORD)
_declare(_user32.MonitorFromPoint, wintypes.HMONITOR, wintypes.POINT, _DWORD)
_declare(_user32.GetMonitorInfoW, _BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_declare(_user32.GetCursorPos, _BOOL, _POINT_P)
_declare(_user32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID)
_declare(_user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_declare(_user32.UnregisterClassW, _BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_declare(
    _user32.CreateWindowExW,
    _HWND, _DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, _DWORD, _INT, _INT, _INT, _INT,
    _HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)
_declare(_user32.DefWindowProcW, LRESULT, _HWND, _UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(_user32.DestroyWindow, _BOOL, _HWND)
_declare(_user32.ShowWindow, _BOOL, _HWND, _INT)
_declare(_user32.SetWindowPos, _BOOL, _HWND, _HWND, _INT, _INT, _INT, _INT, _UINT)
_declare(
    _user32.UpdateLayeredWindow,
    _BOOL, _HWND, _HDC, _POINT_P, ctypes.POINTER(wintypes.SIZE), _HDC, _POINT_P,
    wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), _DWORD,
)
_declare(_user32.GetDC, _HDC, _HWND)
_declare(_user32.ReleaseDC, _INT, _HWND, _HDC)
_declare(_user32.GetMessageW, _INT, ctypes.POINTER(wintypes.MSG), _HWND, _UINT, _UINT)
_declare(_user32.TranslateMessage, _BOOL, ctypes.POINTER(wintypes.MSG))
_declare(_user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_declare(_user32.PostThreadMessageW, _BOOL, _DWORD, _UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(_user32.PostMessageW, _BOOL, _HWND, _UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(_user32.SetTimer, wintypes.WPARAM, _HWND, wintypes.WPARAM, _UINT, wintypes.LPVOID)
_declare(_user32.KillTimer, _BOOL, _HWND, wintypes.WPARAM)
_declare(_user32.SetWinEventHook, wintypes.HANDLE, _DWORD, _DWORD, wintypes.HMODULE, WINEVENTPROC, _DWORD, _DWORD, _DWORD)
_declare(_user32.UnhookWinEvent, _BOOL, wintypes.HANDLE)
_declare(_user32.SetCapture, _HWND, _HWND)
_declare(_user32.ReleaseCapture, _BOOL)
_declare(_gdi32.CreateCompatibleDC, _HDC, _HDC)
_declare(
    _gdi32.CreateDIBSection,
    wintypes.HBITMAP, _HDC, ctypes.POINTER(BITMAPINFOHEADER), _UINT, ctypes.POINTER(wintypes.LPVOID),
    wintypes.HANDLE, _DWORD,
)
_declare(_gdi32.SelectObject, wintypes.HGDIOBJ, _HDC, wintypes.HGDIOBJ)
_declare(_gdi32.DeleteObject, _BOOL, wintypes.HGDIOBJ)
_declare(_gdi32.DeleteDC, _BOOL, _HDC)
_declare(_kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
_declare(_kernel32.GetCurrentThreadId, _DWORD)
_declare(_kernel32.CreateMutexW, wintypes.HANDLE, wintypes.LPVOID, _BOOL, wintypes.LPCWSTR)
_declare(_kernel32.CreateEventW, wintypes.HANDLE, wintypes.LPVOID, _BOOL, _BOOL, wintypes.LPCWSTR)
_declare(_kernel32.OpenEventW, wintypes.HANDLE, _DWORD, _BOOL, wintypes.LPCWSTR)
_declare(_kernel32.SetEvent, _BOOL, wintypes.HANDLE)
_declare(_kernel32.WaitForSingleObject, _DWORD, wintypes.HANDLE, _DWORD)
_declare(_kernel32.CloseHandle, _BOOL, wintypes.HANDLE)
_declare(
    _shcore.GetDpiForMonitor,
    ctypes.c_long, wintypes.HMONITOR, _INT, ctypes.POINTER(_UINT), ctypes.POINTER(_UINT),
)
_declare(_dwmapi.DwmSetWindowAttribute, ctypes.c_long, _HWND, _DWORD, wintypes.LPCVOID, _DWORD)


def enable_dpi_awareness() -> None:
    """Work in real pixels on every monitor, whatever its scaling (must run before tkinter starts).

    Without this Windows reports stretched coordinates for monitors whose
    scaling differs from the main one, and the taskbar strip would land in the
    wrong place there.
    """
    try:
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2, Windows 10 1703+
            return
    except AttributeError:
        pass
    try:
        _shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        pass


def _personalize_flag(name: str) -> int | None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def system_uses_dark_theme() -> bool:
    """The light/dark choice for app windows."""
    return _personalize_flag("AppsUseLightTheme") in (0, None)


def taskbar_is_dark() -> bool:
    """The light/dark choice for the taskbar, which Windows lets you set separately from apps."""
    return _personalize_flag("SystemUsesLightTheme") in (0, None)


def window_rect(hwnd: int | None) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not hwnd or not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def monitor_scale(monitor: int) -> float:
    """1.0 at 100 % display scaling, 1.5 at 150 %."""
    dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
    _shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))  # MDT_EFFECTIVE_DPI
    return (dpi_x.value or 96) / 96


def monitor_at_cursor() -> tuple[tuple[int, int, int, int], float]:
    """Work area (the monitor minus its taskbar) and scale of the monitor the mouse is on."""
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    monitor = _user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    work = info.rcWork
    return (work.left, work.top, work.right, work.bottom), monitor_scale(monitor)


def round_window_corners(window: tk.Misc) -> None:
    """Ask Windows 11 for rounded corners and a shadow; does nothing on Windows 10."""
    hwnd = _user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT
    preference = ctypes.c_int(2)  # DWMWCP_ROUND
    _dwmapi.DwmSetWindowAttribute(
        hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)  # DWMWA_WINDOW_CORNER_PREFERENCE
    )


def acquire_single_instance() -> int | None:
    """Hold a named mutex for the life of the process; None when another copy already has it."""
    handle = _kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}")
    return None if ctypes.get_last_error() == 183 else handle  # ERROR_ALREADY_EXISTS


QUIT_EVENT = f"Local\\{APP_ID}.quit"


def wait_for_quit_request() -> bool:
    """Block until another copy asks this one to exit (`--quit`). False if the wait could not be set up."""
    handle = _kernel32.CreateEventW(None, True, False, QUIT_EVENT)
    return bool(handle) and _kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0  # INFINITE; 0 = signalled


def request_quit() -> bool:
    """Ask the running copy to exit. False when there is none."""
    handle = _kernel32.OpenEventW(0x0002, False, QUIT_EVENT)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    _kernel32.SetEvent(handle)
    _kernel32.CloseHandle(handle)
    return True


def paint_layered_window(hwnd: int, image: Image.Image, x: int, y: int) -> None:
    """Make an RGBA image the whole content of a layered window at (x, y), blended per pixel with what is under it."""
    width, height = image.size
    pixels = image.convert("RGBa").tobytes("raw", "BGRa")  # Windows wants premultiplied BGRA
    header = BITMAPINFOHEADER(
        biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height, biPlanes=1, biBitCount=32
    )  # negative height: rows run top to bottom, as in the image
    bits = wintypes.LPVOID()
    screen_dc = _user32.GetDC(None)
    memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
    bitmap = _gdi32.CreateDIBSection(memory_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    try:
        if not bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(bits, pixels, len(pixels))
        previous = _gdi32.SelectObject(memory_dc, bitmap)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, whole-window alpha 255, AC_SRC_ALPHA
        painted = _user32.UpdateLayeredWindow(
            hwnd,
            screen_dc,
            ctypes.byref(wintypes.POINT(x, y)),
            ctypes.byref(wintypes.SIZE(width, height)),
            memory_dc,
            ctypes.byref(wintypes.POINT(0, 0)),
            0,
            ctypes.byref(blend),
            2,  # ULW_ALPHA
        )
        error = ctypes.get_last_error()
        _gdi32.SelectObject(memory_dc, previous)
        if not painted:
            raise ctypes.WinError(error)
    finally:
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory_dc)
        _user32.ReleaseDC(None, screen_dc)


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    # pythonw has no console, so anything unhandled must land in the log file
    def log_unhandled(exc_type, exc, traceback):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, traceback))

    sys.excepthook = log_unhandled
    threading.excepthook = lambda args: log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


# ----------------------------------------------------------------------------
# Polling
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PollResult:
    usage: Usage | None
    error: str | None
    next_poll: datetime


class Poller(threading.Thread):
    """Fetches usage on the configured interval and publishes a PollResult each time."""

    def __init__(self, config: Config, publish):
        super().__init__(name="poller", daemon=True)
        self.config = config
        self.publish = publish
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._refused_until = 0.0  # while the service has told us to stay away (time.monotonic)

    def refresh_now(self) -> bool:
        """Fetch now instead of waiting out the interval. False, and nothing happens, while rate limited:
        asking again early only makes the service stay closed for longer."""
        if time.monotonic() < self._refused_until:
            return False
        self._wake.set()
        return True

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def run(self) -> None:
        failures = refusals = 0
        while not self._stopping.is_set():
            delay = self.config.refresh_minutes * 60
            usage = error = None
            try:
                creds = load_credentials()
                usage = parse_usage(request_usage(creds), datetime.now(timezone.utc), creds.plan)
                failures = refusals = 0
                log.info("Usage: %s", ", ".join(f"{limit.key} {limit.percent:.0f}%" for limit in usage.limits))
            except UsageError as exc:
                failures += 1
                error = str(exc)
                if exc.retry_after_s:
                    refusals += 1
                    delay = rate_limit_wait(delay, exc.retry_after_s, refusals)
                    self._refused_until = time.monotonic() + delay
                elif exc.transient and failures <= QUICK_RETRIES:
                    delay = QUICK_RETRY_S
                log.warning("Poll failed (%d in a row): %s - next try in %.0f s", failures, exc, delay, exc_info=exc.__cause__)
            except Exception:
                failures += 1
                error = "Unexpected error - see the log file"
                log.exception("Poll failed unexpectedly")
            self.publish(PollResult(usage, error, datetime.now(timezone.utc) + timedelta(seconds=delay)))
            self._wake.wait(delay)
            self._wake.clear()


# ----------------------------------------------------------------------------
# Taskbar strip
# ----------------------------------------------------------------------------

STRIP_WINDOW_CLASS = f"{APP_ID}Strip"
WM_TIMER, WM_QUIT, WM_SYNC = 0x0113, 0x0012, 0x8001  # WM_SYNC is WM_APP + 1: "state changed, redraw"
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP, WM_CAPTURECHANGED = 0x0200, 0x0201, 0x0202, 0x0205, 0x0215
WS_POPUP = 0x80000000
WS_EX_STRIP = 0x00080000 | 0x00000080 | 0x00000008 | 0x08000000  # LAYERED, TOOLWINDOW, TOPMOST, NOACTIVATE
WS_EX_TRANSPARENT = 0x00000020  # the mouse goes straight through to whatever is underneath
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010


@dataclass(frozen=True)
class Taskbar:
    hwnd: int
    primary: bool
    rect: tuple[int, int, int, int]
    anchor: int  # x where the notification area (main taskbar) or the clock (other monitors) begins
    scale: float
    icons_end: int | None = None  # x where the app buttons stop, when that can be worked out


def find_taskbars() -> list[Taskbar]:
    """Every horizontal taskbar: the main one and the one Windows puts on each other monitor."""
    bars = []
    for class_name, anchor_class, fallback, primary in (
        ("Shell_TrayWnd", "TrayNotifyWnd", PRIMARY_TRAY_WIDTH, True),
        ("Shell_SecondaryTrayWnd", "ClockButton", SECONDARY_CLOCK_WIDTH, False),
    ):
        hwnd = None
        while hwnd := _user32.FindWindowExW(None, hwnd, class_name, None):
            rect = window_rect(hwnd)
            if rect is None or not _user32.IsWindowVisible(hwnd):
                continue
            left, top, right, bottom = rect
            if right - left <= bottom - top:
                continue  # vertical taskbar (Windows 10): two rows of text do not fit across it
            scale = monitor_scale(_user32.MonitorFromWindow(hwnd, 2))
            child = window_rect(_user32.FindWindowExW(hwnd, None, anchor_class, None))
            anchor = child[0] if child and left < child[0] < right else right - round(fallback * scale)
            # Centered buttons (the Windows 11 default) are mirrored about the middle of the taskbar, so
            # where the group begins - the Start button - gives away where it ends. Left-aligned: unknown.
            start = window_rect(_user32.FindWindowExW(hwnd, None, "Start", None))
            centered = start is not None and start[0] - left > (right - left) / 10
            bars.append(Taskbar(hwnd, primary, rect, anchor, scale, left + right - start[0] if centered else None))
    return bars


def strip_unit(bar: Taskbar) -> float:
    """Screen pixels per design pixel: the monitor's scale, or less on a taskbar too short for two rows."""
    return min(bar.scale, (bar.rect[3] - bar.rect[1]) / STRIP_DESIGN_HEIGHT)


def strip_geometry(bar: Taskbar, gap: float | None, design_width: int) -> tuple[int, int, int, int]:
    """Where the strip sits on a taskbar: x, y, width, height. `gap` is the dragged position, if any."""
    left, top, right, bottom = bar.rect
    width = round(design_width * strip_unit(bar))
    if gap is None:
        gap = STRIP_GAP_PRIMARY if bar.primary else STRIP_GAP_SECONDARY
    x = bar.anchor - round(gap * bar.scale) - width
    return min(max(x, left), right - width), top, width, bottom - top


def strip_gap(bar: Taskbar, x: int, width: int) -> float:
    """The inverse of strip_geometry: the gap that puts the strip at x."""
    return (bar.anchor - x - width) / bar.scale


def mascot_unit(bar: Taskbar) -> int:
    """Screen pixels per mascot pixel on this taskbar."""
    return max(round(MASCOT_UNIT * strip_unit(bar)), 1)


def mascot_pen(bar: Taskbar, strip_x: int, strip_width: int, above: bool) -> tuple[int, int, int]:
    """Where the mascot's window may be: its lowest x, its highest x, and its y.

    Above: it stands on the taskbar's top edge and goes no further left or
    right than the readout does.

    Otherwise it stands inside the taskbar and gets the stretch just left of
    the readout: as much as is free of app buttons when that is known, within
    limits. If the readout has been dragged to the far left it takes the
    right-hand side instead.
    """
    unit, scale = mascot_unit(bar), strip_unit(bar)
    body, air = 16 * unit, MASCOT_ORIGIN[0] * unit  # the logo itself, and the empty margin beside it in its window
    if above:
        low = strip_x - air
        return low, max(strip_x + strip_width - body - air, low), bar.rect[1] - MASCOT_CANVAS[1] * unit
    free = strip_x - bar.icons_end if bar.icons_end is not None else MASCOT_PEN_MAX * scale
    span = round(min(max(free, MASCOT_PEN_MIN * scale), MASCOT_PEN_MAX * scale))
    gap = round(MASCOT_GAP * scale)
    span = max(span, body + gap)  # never less than standing room
    y = bar.rect[3] - round(MASCOT_FEET * scale) - MASCOT_CANVAS[1] * unit
    if strip_x - span >= bar.rect[0]:
        return strip_x - span - air, strip_x - gap - body - air, y
    after = strip_x + strip_width
    return after + gap - air, after + span - body - air, y


def taskbar_is_showing(bar: Taskbar) -> bool:
    """False while a full-screen app covers the taskbar or auto-hide has slid it off the screen."""
    left, top, right, bottom = bar.rect
    for fraction in (0.03, 0.25, 0.5):  # three probes: the strip itself can only be sitting on one
        point = wintypes.POINT(left + int((right - left) * fraction), (top + bottom) // 2)
        window = _user32.WindowFromPoint(point)
        if window and _user32.GetAncestor(window, 2) == bar.hwnd:  # GA_ROOT
            return True
    return False


def _is_below(window: int, other: int) -> bool:
    """True when `other` is above `window` in the stacking order."""
    for _ in range(500):
        window = _user32.GetWindow(window, 3)  # GW_HWNDPREV: the next window up
        if not window:
            return False
        if window == other:
            return True
    return False


@dataclass
class _Overlay:
    """The readout window on one taskbar."""

    hwnd: int
    bar: Taskbar
    x: int = 0
    width: int = 0
    drawn: tuple | None = None  # what is on screen now, so an unchanged state is not repainted
    shown: bool = False
    drag: tuple[int, int] | None = None  # cursor x and strip x when the mouse button went down
    dragged: bool = False


@dataclass
class _Pet:
    """The mascot keeping the readout company on one taskbar."""

    hwnd: int
    bar: Taskbar
    x: float = 0.0  # left edge of its window
    direction: int = 1
    mood: str = ""
    action: str = "rest"  # or "walk"
    until: float = 0.0  # when it picks the next thing to do
    frame: int = 0
    mood_frame: int = 0  # frames since its mood last changed
    alert_left: int = 0  # frames still to play of the jump that announces a new percent
    drawn: tuple | None = None
    shown: bool = False


def step_pet(
    pet: _Pet, mood_name: str, now: float, elapsed: float, rng: random.Random, pen: tuple[float, float]
) -> Pose:
    """Advance the mascot one animation frame: decide what it does, move it within `pen`, return how it looks."""
    mood = MOODS[mood_name]
    pet.frame += 1
    pet.mood_frame += 1
    if mood_name != pet.mood:  # the usage crossed into another band: drop everything
        pet.mood, pet.until, pet.mood_frame = mood_name, 0.0, 0
    if mood_name == "dead":
        pet.alert_left = 0  # the dead announce nothing
    alert_frame = None
    if pet.alert_left:  # announcing a new percent comes before whatever else it was doing
        alert_frame = ALERT_FRAMES - pet.alert_left
        pet.alert_left -= 1
    elif not mood.speed:
        pet.action = "rest"
    else:
        if now >= pet.until:
            if pet.action == "walk" and mood.rest_s[1] > 0:
                pet.action, pet.until = "rest", now + rng.uniform(*mood.rest_s)
            else:
                pet.action, pet.until = "walk", now + rng.uniform(*mood.walk_s)
                if rng.random() < 0.5:
                    pet.direction = -pet.direction
        if pet.action == "walk":
            pet.x += pet.direction * mood.speed * pet.bar.scale * elapsed
    low, high = pen
    if pet.x <= low:  # also when the pen itself moved: the readout was dragged, or app buttons arrived
        pet.x, pet.direction = low, 1
    elif pet.x >= high:
        pet.x, pet.direction = high, -1
    if alert_frame is not None:
        return mascot_pose(mood_name, "alert", alert_frame, pet.direction)
    frame = pet.mood_frame if mood_name in ("dead", "confused") else pet.frame
    return mascot_pose(mood_name, pet.action, frame, pet.direction)


class TaskbarStrip(threading.Thread):
    """The always-visible readout on every monitor's taskbar, beside the clock, and the mascot that goes with it.

    A tray icon cannot do this job: it is 16 pixels, and Windows shows tray
    icons on the main monitor only. So each taskbar gets a small borderless
    window floating just above it. The windows are not children of the taskbar
    (that would tie Explorer's responsiveness to this process); instead a
    timer keeps them in place, hides them when the taskbar is covered or
    hidden, and lifts them back when the taskbar is raised over them.

    Left-click on the readout opens the details window, right-click opens the
    tray menu, and dragging sideways moves it along the taskbar. The mascot
    ignores the mouse completely, so whatever is under it still takes the click.
    """

    def __init__(self, config: Config, on_click, on_menu):
        super().__init__(name="taskbar-strip", daemon=True)
        self.config = config
        self._on_click = on_click
        self._on_menu = on_menu
        self._state: tuple[Usage | None, bool] = (None, False)
        self._overlays: dict[int, _Overlay] = {}  # keyed by taskbar window
        self._pets: dict[int, _Pet] = {}  # keyed by taskbar window
        self._by_window: dict[int, _Overlay] = {}  # keyed by the overlay's own window
        self._random = random.Random()
        self._thread_id = 0
        self._frame_timer = 0
        self._last_frame = 0.0
        self._announced: dict[str, int] = {}  # per limit, the last percent above 90 the mascot jumped for
        self._alert = False  # set when there is a new one; an animation frame starts the jump
        self._last_alert = float("-inf")  # when the last jump began (time.monotonic)
        self._busy = False
        self._last_error = ""
        # ctypes callbacks must stay referenced for as long as Windows may call them
        self._window_proc_ref = WNDPROC(self._window_proc)
        self._event_proc_ref = WINEVENTPROC(lambda *event: self._safely(self._sync))

    # -- called from other threads ---------------------------------------------

    def update(self, usage: Usage | None, stale: bool) -> None:
        percent = new_alert(usage, stale, self._announced)
        if percent:
            log.info("A limit reached %d%% - the mascot will jump", percent)
            self._alert = True
        self._state = (usage, stale)
        self.request_sync()

    def request_sync(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_SYNC, 0, 0)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # -- the strip's own thread --------------------------------------------------

    def run(self) -> None:
        instance = _kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            lpfnWndProc=self._window_proc_ref,
            hInstance=instance,
            hCursor=_user32.LoadCursorW(None, 32512),  # IDC_ARROW
            lpszClassName=STRIP_WINDOW_CLASS,
        )
        if not _user32.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._instance = instance
        # the foreground changing is when the taskbar tends to be raised over the strip or covered by a full-screen app
        hook = _user32.SetWinEventHook(3, 3, None, self._event_proc_ref, 0, 0, 0)  # EVENT_SYSTEM_FOREGROUND
        sync_timer = _user32.SetTimer(None, 0, STRIP_SYNC_MS, None)
        self._thread_id = _kernel32.GetCurrentThreadId()
        try:
            self._safely(self._sync)
            message = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.hWnd is not None:
                    _user32.TranslateMessage(ctypes.byref(message))
                    _user32.DispatchMessageW(ctypes.byref(message))
                elif message.message == WM_TIMER and message.wParam == self._frame_timer:
                    self._safely(self._animate)
                elif message.message in (WM_TIMER, WM_SYNC):
                    self._safely(self._sync)
        finally:
            self._thread_id = 0
            _user32.KillTimer(None, sync_timer)
            if self._frame_timer:
                _user32.KillTimer(None, self._frame_timer)
            if hook:
                _user32.UnhookWinEvent(hook)
            self._reconcile(self._overlays, {}, _Overlay, 0)
            self._reconcile(self._pets, {}, _Pet, 0)
            _user32.UnregisterClassW(STRIP_WINDOW_CLASS, instance)

    def _safely(self, work) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            work()
            self._last_error = ""
        except Exception as exc:
            if repr(exc) != self._last_error:  # this runs many times a second: log a failure once, not forever
                self._last_error = repr(exc)
                log.exception("Taskbar strip update failed")
        finally:
            self._busy = False

    def _reconcile(self, windows: dict, bars: dict[int, Taskbar], kind, extra_style: int) -> None:
        """Make `windows` hold exactly one window per taskbar in `bars`."""
        for taskbar in [hwnd for hwnd in windows if hwnd not in bars]:
            window = windows.pop(taskbar)
            self._by_window.pop(window.hwnd, None)
            _user32.DestroyWindow(window.hwnd)
        for taskbar, bar in bars.items():
            if taskbar in windows:
                windows[taskbar].bar = bar
                continue
            hwnd = _user32.CreateWindowExW(
                WS_EX_STRIP | extra_style, STRIP_WINDOW_CLASS, APP_NAME, WS_POPUP, 0, 0, 0, 0,
                None, None, self._instance, None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            windows[taskbar] = window = kind(hwnd, bar)
            if kind is _Overlay:
                self._by_window[hwnd] = window  # only the readout takes mouse input
            log.info(
                "%s added on the %s taskbar at %s",
                "Readout" if kind is _Overlay else "Mascot",
                "main" if bar.primary else "secondary",
                bar.rect,
            )

    def _sync(self) -> None:
        """Every 300 ms: follow the taskbars, redraw the readout if it changed, fix visibility and stacking."""
        config = self.config
        bars = {bar.hwnd: bar for bar in find_taskbars()} if config.taskbar_strip or config.mascot else {}
        self._reconcile(self._overlays, bars if config.taskbar_strip else {}, _Overlay, 0)
        self._reconcile(self._pets, bars if config.mascot else {}, _Pet, WS_EX_TRANSPARENT)
        if bool(self._pets) != bool(self._frame_timer):  # only tick the animation while there is a mascot
            if self._pets:
                self._frame_timer, self._last_frame = _user32.SetTimer(None, 0, MASCOT_FRAME_MS, None), time.monotonic()
            else:
                _user32.KillTimer(None, self._frame_timer)
                self._frame_timer = 0

        if not self._pets:
            self._alert = False  # nobody to do the jump; do not save it up for later

        usage, stale = self._state
        dark = taskbar_is_dark()
        showing = {hwnd: taskbar_is_showing(bar) for hwnd, bar in bars.items()}
        for overlay in self._overlays.values():
            bar = overlay.bar
            if overlay.drag is None:
                x, y, width, height = strip_geometry(bar, self._gap(bar), strip_design_width(usage))
                readings = tuple(round(limit.percent) for limit in usage.limits) if usage else None
                state = (x, y, width, height, dark, stale, readings)
                if state != overlay.drawn:
                    image = render_strip(width, height, strip_unit(bar), usage, stale, dark)
                    paint_layered_window(overlay.hwnd, image, x, y)
                    overlay.x, overlay.width, overlay.drawn = x, width, state
        for window in (*self._overlays.values(), *self._pets.values()):
            self._set_shown(window, showing[window.bar.hwnd] and window.drawn is not None)  # not before a first paint
            if window.shown and _is_below(window.hwnd, window.bar.hwnd):
                _user32.SetWindowPos(window.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _alert_due(self, now: float) -> bool:
        """True once per waiting announcement, and never more often than every few minutes."""
        if not self._alert or now - self._last_alert < SIGN_EVERY_S:
            return False
        self._alert, self._last_alert = False, now
        return True

    def _gap(self, bar: Taskbar) -> float | None:
        """Where the user dragged the readout on this kind of taskbar, if they did."""
        return self.config.strip_gap_primary if bar.primary else self.config.strip_gap_secondary

    @staticmethod
    def _set_shown(window, showing: bool) -> None:
        if showing != window.shown:
            _user32.ShowWindow(window.hwnd, 4 if showing else 0)  # SW_SHOWNOACTIVATE / SW_HIDE
            window.shown = showing

    def _animate(self) -> None:
        """Every 70 ms: let each mascot take a step and repaint it if it moved or changed."""
        now = time.monotonic()
        elapsed = min(now - self._last_frame, 0.25)  # after a stall, carry on rather than leap
        self._last_frame = now
        mood = mascot_mood(*self._state)
        alert = self._alert_due(now)
        for pet in self._pets.values():
            if alert:
                pet.alert_left = ALERT_FRAMES  # a hidden mascot keeps it and jumps when it is back
            bar = pet.bar
            overlay = self._overlays.get(bar.hwnd)
            if overlay and overlay.width:
                strip_x, strip_width = overlay.x, overlay.width
            else:  # readout switched off: the mascot keeps its place beside where it would be
                strip_x, _, strip_width, _ = strip_geometry(bar, self._gap(bar), strip_design_width(self._state[0]))
            low, high, y = mascot_pen(bar, strip_x, strip_width, self.config.mascot_above)
            if pet.drawn is None:  # new arrival
                pet.x = (low + high) / 2
            elif not pet.shown:
                continue  # the taskbar is covered or hidden: stand still until it is back
            pose = step_pet(pet, mood, now, elapsed, self._random, (low, high))
            x = round(pet.x)
            trembling = pet.action == "rest" and mood in ("nervous", "stressed")
            if trembling or (mood == "dead" and pet.mood_frame < DEATH_SHOCK):
                x += pet.frame % 2 * max(round(bar.scale), 1)
            unit = mascot_unit(bar)
            state = (unit, pose, x, y)
            if state != pet.drawn:
                paint_layered_window(pet.hwnd, render_mascot(unit, pose), x, y)
                pet.drawn = state

    def _window_proc(self, hwnd, message, wparam, lparam):
        overlay = self._by_window.get(hwnd)
        if overlay is not None:
            try:
                if self._handle_mouse(overlay, message):
                    return 0
            except Exception:
                log.exception("Taskbar strip mouse handling failed")
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _handle_mouse(self, overlay: _Overlay, message: int) -> bool:
        if message == WM_LBUTTONDOWN:
            _user32.SetCapture(overlay.hwnd)  # keep receiving the mouse while it is dragged off the strip
            overlay.drag, overlay.dragged = (self._cursor_x(), overlay.x), False
        elif message == WM_MOUSEMOVE and overlay.drag:
            start_cursor, start_x = overlay.drag
            moved = self._cursor_x() - start_cursor
            if overlay.dragged or abs(moved) > STRIP_DRAG_THRESHOLD * overlay.bar.scale:
                left, top, right, _ = overlay.bar.rect
                overlay.dragged = True
                overlay.x = min(max(start_x + moved, left), right - overlay.width)
                _user32.SetWindowPos(overlay.hwnd, None, overlay.x, top, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        elif message == WM_LBUTTONUP and overlay.drag:
            dragged = overlay.dragged
            _user32.ReleaseCapture()  # sends WM_CAPTURECHANGED, which clears the drag
            if dragged:
                gap = strip_gap(overlay.bar, overlay.x, overlay.width)
                setattr(self.config, "strip_gap_primary" if overlay.bar.primary else "strip_gap_secondary", gap)
                self.config.save()
                log.info("Taskbar strip moved: %.0f px from the clock", gap)
            else:
                self._on_click()
        elif message == WM_CAPTURECHANGED:
            overlay.drag, overlay.dragged, overlay.drawn = None, False, None  # redraw at the settled position
        elif message == WM_RBUTTONUP:
            self._on_menu()
        else:
            return False
        return True

    @staticmethod
    def _cursor_x() -> int:
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return point.x


# ----------------------------------------------------------------------------
# Popup window
# ----------------------------------------------------------------------------


class _LimitRow:
    """Name, percentage, progress bar and reset line for one limit."""

    def __init__(self, popup: Popup, parent: tk.Widget, primary: bool):
        self.popup = popup
        self.primary = primary
        self.bar_height = popup.px(8 if primary else 4)
        theme = popup.theme
        frame = tk.Frame(parent, bg=theme.bg)
        frame.pack(fill="x", pady=(popup.px(14 if primary else 10), 0))
        top = tk.Frame(frame, bg=theme.bg)
        top.pack(fill="x")
        size = 10 if primary else 9
        self.name = popup.label(top, size=size, color=theme.text if primary else theme.secondary)
        self.name.pack(side="left")
        self.percent = popup.label(top, size=size, bold=True)
        self.percent.pack(side="right")
        self.bar = tk.Label(frame, bg=theme.bg, bd=0)
        self.bar.pack(pady=(popup.px(6 if primary else 4), 0))
        self.detail = popup.label(frame, color=theme.muted)
        if primary:
            self.detail.pack(anchor="w", pady=(popup.px(4), 0))

    def update(self, limit: Limit, now: datetime, stale: bool) -> None:
        popup = self.popup
        reset = describe_reset(limit, now, popup.twelve_hour)
        if self.primary:
            self.name.config(text=limit.label)
            self.detail.config(text=reset)
        else:
            countdown = format_countdown(limit.resets_at, now)
            self.name.config(
                text=f"{limit.label} · resets in {countdown}" if countdown not in ("", "now") else limit.label
            )
        self.percent.config(text=f"{limit.percent:.0f}%")
        fill = popup.theme.muted if stale else STATUS_COLORS[severity(limit.percent)]
        popup.set_image(
            self.bar,
            bar_image(popup.content_width, self.bar_height, limit.percent, fill, popup.theme.track, popup.theme.bg),
        )


class Popup:
    """Borderless always-on-top details window; built fresh each time it is opened.

    It is laid out for one monitor - `work_area` and `scale` say which - so it
    comes out the right size on a screen whose scaling differs from the main one.
    """

    def __init__(self, root: tk.Tk, theme: Theme, work_area: tuple[int, int, int, int], scale: float, on_refresh, on_close):
        self.theme = theme
        self.work_area = work_area
        self.scale = scale
        self.twelve_hour = uses_12_hour_clock()
        self._on_refresh = on_refresh
        self._images: dict[str, ImageTk.PhotoImage] = {}  # tkinter drops images nobody references
        self._rows: dict[str, _LimitRow] = {}
        self._drag_offset = (0, 0)
        self._dragged = False

        self.win = win = tk.Toplevel(root, bg=theme.border)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.bind("<Escape>", lambda event: on_close())
        body = tk.Frame(win, bg=theme.bg)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        pad = self.px(16)
        self.content_width = self.px(POPUP_WIDTH) - 2 * pad

        header = tk.Frame(body, bg=theme.bg)
        header.pack(fill="x", padx=(pad, self.px(8)), pady=(self.px(10), 0))
        self.logo = tk.Label(header, bg=theme.bg, bd=0)
        self.logo.pack(side="left", padx=(0, self.px(8)))
        title = self.label(header, APP_NAME, size=11, bold=True)
        title.pack(side="left")
        self.plan = self.label(header, color=theme.muted)
        self.plan.pack(side="left", padx=(self.px(8), 0))
        close = self._button(header, "✕", on_close, padx=self.px(8))
        close.config(bg=theme.bg, fg=theme.secondary)
        close.bind("<Leave>", lambda event: close.config(bg=theme.bg))
        close.pack(side="right")
        for widget in (header, self.logo, title, self.plan):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

        # zero-height strut: the popup keeps its width even before there are bars to show
        tk.Frame(body, bg=theme.bg, width=self.content_width, height=0).pack(padx=pad)
        self.rows = tk.Frame(body, bg=theme.bg)
        self.rows.pack(fill="x", padx=pad)
        self.label(self.rows, "Waiting for the first reading...", color=theme.muted).pack(
            anchor="w", pady=(self.px(14), 0)
        )

        chart_head = tk.Frame(body, bg=theme.bg)
        chart_head.pack(fill="x", padx=pad, pady=(self.px(18), self.px(6)))
        self.label(chart_head, f"Last {HISTORY_HOURS} hours, 0-100%", color=theme.secondary).pack(side="left")
        for name, color in reversed(list(zip(SHORT_LABELS.values(), theme.series))):
            self.label(chart_head, name, color=theme.muted).pack(side="right", padx=(self.px(4), 0))
            swatch = tk.Frame(chart_head, bg=color, width=self.px(10), height=self.px(3))
            swatch.pack(side="right", padx=(self.px(12), 0))
        self.chart = self.label(body, color=theme.muted)
        self.chart.pack(padx=pad, anchor="w")

        self.footer = tk.Frame(body, bg=theme.bg)
        self.footer.pack(fill="x", padx=pad, pady=(self.px(12), self.px(14)))
        self.status = self.label(self.footer, color=theme.muted)
        self.status.pack(side="left")
        self._button(self.footer, "Refresh", self._refresh, padx=self.px(12), pady=self.px(3)).pack(side="right")
        self.error = self.label(body, color=theme.secondary, wraplength=self.content_width, justify="left")

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def label(self, parent, text="", size=9, color=None, bold=False, **options) -> tk.Label:
        pixels = -round(size * 4 / 3 * self.scale)  # negative = pixels: point sizes would follow the main monitor's scaling
        font = (FONT, pixels, "bold") if bold else (FONT, pixels)
        return tk.Label(
            parent, text=text, font=font, bg=self.theme.bg, fg=color or self.theme.text, bd=0, padx=0, pady=0, **options
        )

    def _button(self, parent, text, command, **options) -> tk.Label:
        theme = self.theme
        button = self.label(parent, text, cursor="hand2")
        button.config(bg=theme.button, **options)
        button.bind("<Button-1>", lambda event: command())
        button.bind("<Enter>", lambda event: button.config(bg=theme.button_hover))
        button.bind("<Leave>", lambda event: button.config(bg=theme.button))
        return button

    def set_image(self, widget: tk.Label, image: Image.Image) -> None:
        photo = ImageTk.PhotoImage(image)
        widget.config(image=photo, text="")
        self._images[str(widget)] = photo

    def _start_drag(self, event) -> None:
        self._drag_offset = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag(self, event) -> None:
        self._dragged = True
        self.win.geometry(f"+{event.x_root - self._drag_offset[0]}+{event.y_root - self._drag_offset[1]}")

    def _refresh(self) -> None:
        self.status.config(text=self._on_refresh() or "Refreshing...")  # or why it will not refresh just now

    def update(
        self,
        usage: Usage | None,
        error: str | None,
        stale: bool,
        next_poll: datetime | None,
        history: History,
    ) -> None:
        now = datetime.now(timezone.utc)
        theme = self.theme
        self.plan.config(text=usage.plan if usage else "")
        unit = max(round(MASCOT_UNIT * self.scale), 1)
        portrait = render_mascot(unit, mascot_face(mascot_mood(usage, stale)))
        left, top = MASCOT_ORIGIN[0] * unit, MASCOT_ORIGIN[1] * unit  # cut the logo out of its animation canvas
        self.set_image(self.logo, portrait.crop((left, top, left + 16 * unit, top + 10 * unit)))

        limits = usage.limits if usage else ()
        if limits and tuple(limit.key for limit in limits) != tuple(self._rows):
            for child in self.rows.winfo_children():
                child.destroy()
            self._rows = {
                limit.key: _LimitRow(self, self.rows, primary=limit.key in PRIMARY_KEYS) for limit in limits
            }
        for limit in limits:
            self._rows[limit.key].update(limit, now, stale)

        start = now - timedelta(hours=HISTORY_HOURS)
        samples = history.since(start)
        if len(samples) >= 2:
            self.set_image(
                self.chart,
                history_image(self.content_width, self.px(56), samples, start.timestamp(), now.timestamp(), theme),
            )
        else:
            self._images.pop(str(self.chart), None)
            self.chart.config(image="", text="History appears after a couple of refreshes")

        status = f"Updated {format_clock(usage.fetched_at, self.twelve_hour)}" if usage else "No data yet"
        if next_poll and next_poll > now:
            status += f" · next in {format_countdown(next_poll, now)}"
        self.status.config(text=status)

        if error:
            suffix = ". Showing the last reading." if usage else "."
            self.error.config(text=f"⚠ {error}{suffix}")
            self.error.pack(fill="x", padx=self.px(16), pady=(self.px(12), 0), before=self.footer)
        else:
            self.error.pack_forget()
        if not self._dragged:
            self._place()

    def _place(self) -> None:
        """Bottom-right corner of the monitor's work area, just above the clock."""
        self.win.update_idletasks()
        width, height = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        _, _, right, bottom = self.work_area
        margin = self.px(12)
        self.win.geometry(f"+{right - width - margin}+{bottom - height - margin}")

    def show(self) -> None:
        self.win.deiconify()
        round_window_corners(self.win)
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        self.win.destroy()


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------


class App:
    """Owns the tray icon, the taskbar strip, the popup and the poller.

    tkinter runs on the main thread; pystray, the strip and the poller run on
    their own threads and only ever talk to the UI through the `events` queue.
    """

    def __init__(self):
        self.config = Config.load()
        self.history = History()
        self.usage: Usage | None = None
        self.error: str | None = None
        self.next_poll: datetime | None = None
        self.popup: Popup | None = None
        self.events: queue.Queue = queue.Queue()
        self._running = True
        self._icon_state = None
        self._last_toggle = 0.0
        self._last_refresh = 0.0
        self._last_tick = time.time()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda *exc_info: log.error("UI callback failed", exc_info=exc_info)
        self.icon = pystray.Icon(APP_ID, render_icon(None), build_tooltip(None, None, _now()), self._build_menu())
        self.poller = Poller(self.config, lambda result: self.events.put(("poll", result)))
        self.strip = TaskbarStrip(
            self.config, on_click=lambda: self.events.put(("toggle", None)), on_menu=self._show_tray_menu
        )

    def run(self) -> None:
        log.info("Started %s %s, refreshing every %d min", APP_NAME, VERSION, self.config.refresh_minutes)
        self.icon.run_detached()
        self.strip.start()
        self.poller.start()
        threading.Thread(target=self._exit_when_asked, name="quit-watch", daemon=True).start()
        self.root.after(EVENT_POLL_MS, self._drain_events)
        self.root.after(TICK_MS, self._tick)
        self.root.mainloop()

    # -- tray menu (these callbacks run on pystray's thread) ------------------

    def _build_menu(self) -> pystray.Menu:
        item = pystray.MenuItem
        intervals = sorted({*INTERVAL_CHOICES, self.config.refresh_minutes})
        return pystray.Menu(
            item("Refresh now", lambda: self.events.put(("refresh", None))),
            item("Open full details", lambda: self.events.put(("toggle", None)), default=True),
            pystray.Menu.SEPARATOR,
            item(
                "Settings",
                pystray.Menu(
                    item("Refresh every", None, enabled=False),
                    *(
                        item(
                            f"{minutes} min",
                            self._interval_setter(minutes),
                            checked=lambda _, minutes=minutes: self.config.refresh_minutes == minutes,
                            radio=True,
                        )
                        for minutes in intervals
                    ),
                    pystray.Menu.SEPARATOR,
                    item(
                        "Mascot walks on top of the taskbar",
                        self._toggle("mascot_above"),
                        checked=lambda _: self.config.mascot_above,
                    ),
                    item("Reset taskbar position", self._reset_strip_position),
                    item("Open log file", lambda: os.startfile(LOG_PATH)),
                ),
            ),
            item("Show on taskbar", self._toggle("taskbar_strip"), checked=lambda _: self.config.taskbar_strip),
            item("Show mascot", self._toggle("mascot"), checked=lambda _: self.config.mascot),
            item("Run at Windows startup", self._toggle_startup, checked=lambda _: is_startup_enabled()),
            pystray.Menu.SEPARATOR,
            item("Exit", lambda: self.events.put(("exit", None))),
        )

    def _exit_when_asked(self) -> None:
        if wait_for_quit_request():
            log.info("Another copy asked this one to exit (--quit)")
            self.events.put(("exit", None))

    def _show_tray_menu(self) -> None:
        """Open the tray icon's menu at the mouse, for a right-click on the taskbar strip.

        pystray has no public call for this, so its hidden window is sent the
        same message Windows sends when the tray icon itself is right-clicked.
        """
        try:
            _user32.PostMessageW(self.icon._hwnd, pystray._util.win32.WM_NOTIFY, 0, WM_RBUTTONUP)
        except Exception:  # pystray internals moved: fall back to the details window
            log.warning("Could not open the tray menu from the taskbar strip", exc_info=True)
            self.events.put(("toggle", None))

    def _toggle(self, setting: str):
        """A menu action that flips one of the on/off settings the taskbar strip reads."""

        def flip() -> None:
            setattr(self.config, setting, not getattr(self.config, setting))
            self.config.save()
            log.info("%s turned %s", setting, "on" if getattr(self.config, setting) else "off")
            self.strip.request_sync()

        return flip

    def _reset_strip_position(self) -> None:
        self.config.strip_gap_primary = self.config.strip_gap_secondary = None
        self.config.save()
        self.strip.request_sync()

    def _interval_setter(self, minutes: int):
        def apply() -> None:
            self.config.refresh_minutes = minutes
            self.config.save()
            log.info("Refresh interval set to %d min", minutes)
            self.poller.refresh_now()

        return apply

    def _toggle_startup(self) -> None:
        enable = not is_startup_enabled()
        set_startup(enable)
        log.info("Run at Windows startup %s", "enabled" if enable else "disabled")

    # -- UI thread -------------------------------------------------------------

    def _drain_events(self) -> None:
        while self._running:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(kind, payload)
            except Exception:
                log.exception("Failed to handle %r event", kind)
        if self._running:
            self.root.after(EVENT_POLL_MS, self._drain_events)

    def _handle(self, kind: str, payload) -> None:
        if kind == "poll":
            self._apply_poll(payload)
        elif kind == "toggle":
            self._toggle_popup()
        elif kind == "refresh":
            self._request_refresh()
        elif kind == "exit":
            self._exit()

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.time()
        if now - self._last_tick > 3 * TICK_MS / 1000:
            # the poller's timer does not advance while the PC sleeps; do not wait out the rest of it
            log.info("Clock jumped (resume from sleep?) - refreshing")
            self.poller.refresh_now()
        self._last_tick = now
        self._render()
        self.root.after(TICK_MS, self._tick)

    def _apply_poll(self, result: PollResult) -> None:
        if result.usage:
            self.usage = result.usage
            self.history.add(result.usage)
        self.error = result.error
        self.next_poll = result.next_poll
        self._render()

    def _is_stale(self, now: datetime) -> bool:
        """The last good reading is old enough that its colors should not be trusted."""
        if not (self.usage and self.error):
            return False
        return now - self.usage.fetched_at > timedelta(minutes=max(3 * self.config.refresh_minutes, 10))

    def _render(self) -> None:
        now = _now()
        stale = self._is_stale(now)
        percents = tuple(round(limit.percent) for limit in self.usage.limits) if self.usage else None
        if (stale, percents) != self._icon_state:
            self._icon_state = (stale, percents)
            self.icon.icon = render_icon(self.usage, stale)
        self.icon.title = build_tooltip(self.usage, self.error, now)
        self.strip.update(self.usage, stale)
        if self.popup:
            self.popup.update(self.usage, self.error, stale, self.next_poll, self.history)

    def _toggle_popup(self) -> None:
        now = time.monotonic()
        if now - self._last_toggle < DOUBLE_CLICK_S:
            return  # second half of a double-click: leave the popup as the first click put it
        self._last_toggle = now
        if self.popup:
            self._close_popup()
            return
        theme = DARK if system_uses_dark_theme() else LIGHT
        work_area, scale = monitor_at_cursor()  # open on the screen whose tray icon or strip was clicked
        self.popup = Popup(
            self.root, theme, work_area, scale, on_refresh=self._request_refresh, on_close=self._close_popup
        )
        self._render()
        self.popup.show()

    def _close_popup(self) -> None:
        if self.popup:
            self.popup.close()
            self.popup = None

    def _request_refresh(self) -> str:
        """Fetch a fresh reading now. Returns "" when one is on its way, otherwise why not."""
        now = time.monotonic()
        if now - self._last_refresh < MIN_REFRESH_GAP_S:
            return "Just refreshed"
        if not self.poller.refresh_now():
            return f"Rate limited \u00b7 next try in {format_countdown(self.next_poll, _now())}"
        self._last_refresh = now
        return ""

    def _exit(self) -> None:
        log.info("Exiting")
        self._running = False
        self.poller.stop()
        self.strip.stop()
        self.icon.stop()
        self.root.destroy()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_once() -> int:
    """Print one reading to the console: the quickest check that credentials and the API work."""
    try:
        creds = load_credentials()
        payload = request_usage(creds)
    except UsageError as exc:
        print(f"Error: {exc}")
        return 1
    now = _now()
    twelve_hour = uses_12_hour_clock()
    usage = parse_usage(payload, now, creds.plan)
    print(f"Credentials: {creds.source} (plan: {creds.plan or 'unknown'})")
    for limit in usage.limits:
        print(f"  {limit.label:<20} {limit.percent:5.1f}%  {describe_reset(limit, now, twelve_hour)}")
    print("Raw reply:")
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude usage tray widget for Windows.")
    parser.add_argument("--once", action="store_true", help="print one usage reading and exit")
    parser.add_argument("--quit", action="store_true", help="ask the copy that is running to exit")
    args = parser.parse_args(argv)
    setup_logging()
    if args.once:
        return run_once()
    if args.quit:
        asked = request_quit()
        print("Asked the running copy to exit." if asked else "No copy is running.")
        return 0 if asked else 1
    mutex = acquire_single_instance()  # keep the handle referenced until exit
    if mutex is None:
        log.info("Already running - this copy exits")
        return 0
    enable_dpi_awareness()
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
