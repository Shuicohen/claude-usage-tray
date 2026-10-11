# Claude Usage Tray

A Windows taskbar widget that shows your Claude Code 5-hour and weekly usage at a glance, with a pixel mascot that panics as you near the limit.

> **Unofficial - not made or endorsed by Anthropic.**

![Claude Usage Tray demo](docs/demo.gif)

([Demo video](docs/demo.mp4))

## Download

Grab `ClaudeUsage.exe` from the [latest GitHub Release](https://github.com/Shuicohen/claude-usage-tray/releases/latest). No install needed; see Quick start below. Prefer running from source? Clone this repo and use `ClaudeUsage.bat` or the Setup by hand steps.

> **SmartScreen note:** Windows may show a SmartScreen warning ("Windows protected your PC") because this is a new app and the exe is not signed yet. The full source is here; you can also run from source. See [Privacy and uninstall](#privacy-and-uninstall).

Shows how much of your Claude **5-hour session** limit and **7-day weekly**
limit is used, without hovering or clicking. It signs in with the token Claude
Code already keeps on this PC, so there is no API key to set up.

## Quick start

You need Windows 10 or 11 and Claude Code signed in on this PC (run `claude`
once if you have not). Then either:

- **`ClaudeUsage.exe`**: double-click it. Nothing to install. It is not
  code-signed, so the first time Windows may say "Windows protected your PC";
  choose More info > Run anyway. Keep the file somewhere permanent.
- **The folder with `ClaudeUsage.bat`**: double-click the `.bat`. This
  needs Python 3.10 or newer; the first run sets itself up, which takes about a
  minute and needs the internet.

The readout appears on the taskbar beside the clock. Right-click it for the
menu; tick Run at Windows startup there to keep it across restarts. To stop
it, right-click > Exit.

It only ever reads your own sign-in from your own PC and talks to
`api.anthropic.com`. It shows the usage of whoever is signed in to Claude Code
on the PC it runs on.

- **Taskbar readout, on every screen.** A few lines beside the clock on each
  monitor's taskbar, each reading name, gauge, percentage:

  ```
  5h    ▬▬▬▬▬▬░░░  66%
  7d    ▬▬░░░░░░░  17%
  Fable ░░░░░░░░░   0%
  ```

  The third line appears when your plan has per-model weekly limits; with more
  than one, it shows whichever is fullest. Each gauge is green below 70 %,
  yellow from 70 % to 90 %, red above 90 %, and grey when the reading is out of
  date.
- **Mascot.** The pixel Claude walks on top of the taskbar, back and forth over
  the readout and no further, and acts out the fullest limit shown (see the
  table below). A setting puts it inside the taskbar beside the readout instead.
- **Tray icon.** Two small gauges, 5-hour on the left and 7-day on the right.
  Its tooltip gives both percentages and the time until each resets.
- **Details window.** Left-click the readout or the tray icon: progress bars,
  exact percentages, reset times, per-model weekly limits when your plan has
  them, and a chart of the last 24 hours. It opens on the screen you clicked.
  Drag it by its title; close it with the X, Esc, or another click.
- **Menu.** Right-click the readout or the tray icon: Refresh now, Open full
  details, Settings, Show on taskbar, Show mascot, Run at Windows startup, Exit.

| Usage (fullest limit) | What the mascot does |
|---|---|
| under 25 % | Dozes sitting down, snoring; a slow shuffle now and then |
| 25 to 50 % | Ambles to and fro, stops to blink and wave |
| 50 to 70 % | Paces briskly; the odd drop of sweat |
| 70 to 80 % | Scuttles, trembles when it stops, eyes darting, sweating steadily |
| 80 to 90 % | Faster still, and sweating twice as much |
| over 90 % | Dashes back and forth flailing its arms, mouth open, drenched |
| 100 % | Dies: a moment of horror, a flip onto its back, its soul floats off. It then lies perfectly still until the limit resets, when it gets back up |
| no current reading | Stands looking about; a question mark shows for a few seconds every few minutes |

**Close-to-the-limit alert.** Once a limit is above 90 %, a new percent (91,
92, ... 99) makes the mascot stop and jump three times, arms up, under a red
exclamation mark. To nag rather than pester, it does this at most once every
three minutes. Each percent is announced once; when the limit resets and climbs
again, the announcements start over. At 100 % there is no jump: it dies. The widget can only
notice a new percent when it refreshes, so a fast climb between two refreshes
gets one jump for the newest percent, not one for each. If the taskbar is hidden
at that moment (a full-screen app), it jumps when the taskbar is back.

## Setup by hand

What `ClaudeUsage.bat` does for you, step by step. In PowerShell, from
this folder:

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Check that it can read your sign-in and reach the usage service. This prints one
reading and exits:

```powershell
.venv\Scripts\python claude_usage_tray.py --once
```

Start it. `pythonw` runs it with no console window:

```powershell
.venv\Scripts\pythonw claude_usage_tray.py
```

### Start with Windows

Right-click and tick **Run at Windows startup**. This writes one value,
`ClaudeUsageTray`, under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
holding the full path of `ClaudeUsage.exe`, or of `pythonw.exe` and this
script. Untick it to remove the value.

The value stores absolute paths. Put the file or folder where it will stay
before you tick the box; if you move it later, untick and tick again from the
new place.

## Adjusting it

- **Move the readout**: drag it sideways along the taskbar; the mascot comes
  along. Each kind of taskbar (main screen, other screens) remembers its own
  spot. Settings > Reset taskbar position puts it back.
- **Turn the readout or the mascot off**: untick Show on taskbar or Show mascot.
- **Move the mascot into the taskbar**: untick Settings > Mascot walks on top of
  the taskbar. It then stands inside the taskbar, to the left of the readout.
- **Refresh interval**: Settings > pick one (default 3 minutes, which is also the
  quickest; see Rate limits below). Any whole number of minutes from 3 to 120
  also works by editing `refresh_minutes` in `config.json` and restarting.

Only one copy runs at a time; a second launch exits quietly. Running it with
`--quit` (`ClaudeUsage.exe --quit`, or `pythonw claude_usage_tray.py --quit`)
tells the running copy to exit, which is handy before replacing the file.

## Where things are

| What | Where |
|---|---|
| Sign-in token (read only) | `%CLAUDE_CONFIG_DIR%\.credentials.json` if that variable is set, else `%USERPROFILE%\.claude\.credentials.json` |
| WSL fallback | `\\wsl.localhost\<distro>\home\<user>\.claude\.credentials.json` (and `\root\`) |
| Settings | `%USERPROFILE%\.claude-usage-tray\config.json` |
| Chart history | `%USERPROFILE%\.claude-usage-tray\history.json` |
| Log | `%USERPROFILE%\.claude-usage-tray\claude_usage_tray.log` (rotates at 500 KB, 2 old copies kept) |

These live in the home folder rather than `%APPDATA%` on purpose. Windows
redirects `%APPDATA%` for any program started from a Store-packaged app, and the
Claude desktop app is one, so the widget would otherwise keep two separate sets
of settings depending on where it was launched from.

## How it works, and its limits

- **Usage data** comes from `GET https://api.anthropic.com/api/oauth/usage`, the
  call behind Claude Code's `/usage` screen. It is not a documented public API,
  so Anthropic may change it. The parser accepts both reply shapes seen so far
  and ignores fields it does not know.
- **The token is only read.** The widget never refreshes it and never writes the
  credentials file, so it cannot sign you out of Claude Code. The cost: access
  tokens last a few hours and only Claude Code renews them. If Claude Code has
  not run for a while the widget shows "Sign-in token expired", keeps the last
  reading (grey once it is old), and recovers by itself the next time Claude
  Code runs. The token is sent only to `api.anthropic.com` and is never logged.
- **The readout floats over the taskbar; it is not part of it.** Windows has no
  supported way to add text to the taskbar, and tray icons are 16 pixels and
  appear on the main screen only. So each taskbar gets a small window kept just
  above it. Consequences:
  - It hides while a full-screen app covers the taskbar and while an auto-hide
    taskbar is tucked away.
  - Windows does not know it is there. With many apps open, taskbar buttons can
    end up underneath it; drag it somewhere emptier.
  - On the main screen it sits left of the tray icons, the nearest free spot to
    the clock. On other screens Windows 11 does not report where the clock
    starts, so the default spot is an estimate; drag it if it overlaps.
  - Vertical taskbars (Windows 10) are skipped.
- **The mascot** stands on the taskbar's top edge and only walks the width of
  the readout, so it passes over the bottom few pixels of whatever window is
  there. Clicks go straight through it. The readout and the mascot together
  use about 1 % of one CPU core.
- **The mascot inside the taskbar** (the setting above) paces whatever is free
  between your app buttons and the readout, up to 160 pixels. On a full taskbar
  that can be no more than standing room, and with still more apps open it ends
  up in front of the last app button; the button still works, and dragging the
  readout to an emptier stretch gives it room again. If the readout is dragged
  to the far left, the mascot moves to its right. With left-aligned taskbar
  buttons Windows does not reveal where they end, so it takes the full 160
  pixels.
- **WSL** is checked only when Windows has no usable token, and the result is
  cached for 15 minutes, because looking inside a stopped distro starts it.
- **Rate limits**: the usage service only puts up with being asked every few
  minutes. Asked once a minute it starts refusing ("429") within about five
  minutes, and it then stays touchy for a while. So the widget never refreshes
  more often than every 3 minutes. If it is refused anyway, it shows "Rate
  limited", keeps the last reading, and stays away for 5 minutes, then 10, 20
  and 30 if it is refused again; Refresh does nothing until that wait is over.
  This only affects looking up the usage numbers, not using Claude. Starting
  the widget asks once straight away, so restarting it over and over can set
  it off too.
- **After sleep** it refreshes within about half a minute of waking; a network
  failure is retried after 30 seconds (three times) before falling back to the
  normal interval.
- **Look**: the readout follows the taskbar's light or dark colour and each
  screen's own scaling; the details window follows the app theme and the 12 or
  24-hour clock setting.

## Troubleshooting

| You see | Do this |
|---|---|
| No readout on the taskbar | Right-click the tray icon (behind the `^` arrow if hidden) and tick Show on taskbar. |
| No tray icon | Look behind the `^` arrow; drag the icon onto the taskbar to keep it visible. |
| Readout or mascot overlaps the clock or an app button | Drag the readout sideways. |
| "Not signed in" | Run `claude` and sign in. |
| "Sign-in token expired" | Open Claude Code; the next refresh picks up the new token. |
| "Sign-in token rejected" | Sign in to Claude Code again (`/login`). |
| "Network error (...)" | Check the connection or proxy. The log has the full error. |
| "Rate limited by Anthropic" | Leave it alone: it tries again by itself after the time shown. Restarting or pressing Refresh does not help. |
| Nothing happens on start | Run the `--once` command above from a console to see the error, then read the log. |
| "The filename or extension is too long" | The folder sits too deep for Windows. Move it somewhere short, such as `C:\ClaudeUsageTray`, delete its `.venv` folder, and run `ClaudeUsage.bat` again. |

## Tests

```powershell
.venv\Scripts\python -m pip install pytest
.venv\Scripts\python -m pytest
```

The tests use made-up data and a scratch registry key. They do not call the
network or touch your credentials, settings, or startup entry.

## Contributing

Issues and pull requests are welcome. Please run the tests (see Tests above) before opening a PR; CI runs them on Windows for every push and pull request.

## Privacy and uninstall

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it. Its only network connection is to Anthropic (`api.anthropic.com`), to read your own Claude usage using the sign-in token Claude Code already stores on your PC; that is the purpose of the app. Anthropic's handling of that request is covered by [Anthropic's privacy policy](https://www.anthropic.com/legal/privacy). The app collects no telemetry and sends nothing anywhere else.

To uninstall: quit the app from its tray menu (right-click > Exit), untick "Run at Windows startup" first if you turned it on, and delete the exe or cloned folder.

## License

[MIT](LICENSE) (c) 2026 Yehoshua Cohen
