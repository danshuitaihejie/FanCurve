# FanCurve

English · [简体中文](README.zh-CN.md)

**FanCurve is a Terminal fan controller for Intel Macs.** It maps the average CPU core temperature to each fan's own RPM range, shows live readings and the temperature curve in a TUI, and displays the current temperature in the macOS menu bar. The source remains a single Python script with no third-party dependencies. **Apple Silicon is not supported.**

![FanCurve English demo dashboard](docs/screenshots/fancurve-en.png)

*Captured from `--demo --lang en` with simulated sensors and fans; no real fan was changed.* [Chinese screenshot](docs/screenshots/fancurve-zh.png)

## Try it safely

On an Intel Mac with Python 3.9 or newer, clone the repository and start the hardware-free demo:

```sh
git clone https://github.com/danshuitaihejie/FanCurve.git
cd FanCurve
uv run --no-project fan_curve.py --demo --lang en
```

Use `--monitor` for read-only measurements from the real Mac. A normal launch requests administrator authorization after the first valid reading and then starts automatic fan control:

```sh
uv run --no-project fan_curve.py --monitor --lang en
uv run --no-project fan_curve.py --lang en
```

If `git` is unavailable, download this repository as a ZIP from GitHub. You can also run `python3 fan_curve.py --demo --lang en`; there are no third-party Python dependencies. Double-click `Launch.command` for Chinese or `Launch-English.command` for English. The former defaults to Chinese. Both launchers use `uv` when available and fall back to `python3`. The `--lang auto`, `--lang en`, and `--lang zh` options select the language; direct CLI launches default to Chinese. Both languages share the same fan-control logic and saved curve.

## macOS app package

On macOS with `/usr/bin/python3` 3.9 or newer and Command Line Tools installed, build a local app and ZIP package with:

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-project build_app.py
```

This creates `dist/FanCurve.app` and `dist/FanCurve-macOS.zip`. The app is a macOS `.app` launcher that bundles the script and opens the Terminal TUI plus menu bar app; it uses the system `/usr/bin/python3`, not an embedded Python runtime. You can drag `FanCurve.app` into Applications. On first launch it creates `~/Library/Application Support/FanCurve/FanCurve.terminal` and imports a dedicated Terminal profile named FanCurve. That profile uses a 100×32 window, closes without prompting, and closes the window after a normal program exit. FanCurve selects English or Chinese from the system language or saved app preference by default. Press `l` in the interface to switch languages and save the preference. Only the dedicated FanCurve profile is changed.

The app stores settings in `~/Library/Application Support/FanCurve/settings.json`. Direct TUI commands keep settings in `.fancurve.json` beside the script. To launch the app through its dedicated Terminal profile from the source checkout, run `uv run --no-project fan_curve.py --launch-app --lang en`; the plain TUI commands above remain available. Packages are local ad hoc-signed builds and are not Developer ID signed or notarized, and are not distributed through the App Store.

## How fan control works

| Behavior | Default |
| --- | --- |
| Input | Arithmetic mean of valid CPU core sensors, followed by a rolling average; CPU package temperature is used if core sensors are unavailable. |
| Curve | Start increasing at **50°C** and reach each fan's reported maximum RPM at **80°C**. |
| Smoothing and refresh | 6-second smoothing window; readings and controls refresh every 2 seconds. |
| Hotspot protection | A known CPU/GPU hotspot at **90°C or above** takes priority and requests full speed; the protection clears below **85°C**. |
| Readback | After an SMC write, FanCurve checks the reported mode and target RPM. If the write cannot be confirmed, it stops automatic control and requests system control. |

The large number is the **smoothed average CPU temperature** used by the curve. The fan table distinguishes actual RPM, the current hardware target, and the curve's expected target. The graph is marked as a preview when FanCurve does not control the fans. The interface needs at least **78 columns × 24 rows**.

| Key | Action |
| --- | --- |
| `a` | Enable or retry automatic control. In a demo, this affects only simulated fans. |
| `s` | Request restoration of system fan control. |
| `c` | Edit start temperature, full-speed temperature, and smoothing time. Use arrow keys; Enter saves and Esc cancels. |
| `l` | Switch English / Chinese; app launches save the language preference. |
| `?` | Show help. |
| `q` | Quit and request restoration of system fan control. |

Do not let another fan-control app control the same fans at the same time. If Macs Fan Control is installed, restore its automatic mode and quit it before enabling FanCurve.

## Optional unattended login startup

`--install` sets up a protected helper and a per-user login item. It needs one administrator authorization. The Mac must provide `/usr/bin/python3` 3.9 or newer; if macOS prompts to install Command Line Tools, finish that installation first. The system sudoers file must include `/private/etc/sudoers.d`.

Keep `fan_curve.py` in its final location, then run:

```sh
uv run --no-project fan_curve.py --install --lang en
```

The installer places a root-owned copy of the script and a fixed launcher under `/Library/PrivilegedHelperTools`. It validates a narrow `/private/etc/sudoers.d/fancurve` rule with `visudo`; that rule lets only the installing user start the fixed helper **without arguments** and without another password prompt. It also writes a launcher under `~/Library/Application Support/FanCurve/` and a LaunchAgent under `~/Library/LaunchAgents/`. Missing protected directories are created only after their parent permissions are checked. No administrator password is stored, and installation itself does not start fan control.

On later logins, FanCurve starts after the user logs in, enables control after a valid reading, minimizes its own Terminal window, and keeps the temperature in the menu bar. **Click the temperature to reopen that same window; minimizing it leaves fan control running.** Closing the window or pressing `q` exits. The language selected during `--install` persists for login startup. The login item uses a dedicated Terminal profile and `Launch.command`; it can migrate the older generated Chinese launcher. This is a login item, not a pre-login system service.

If you move the script, change the startup language, or update helper logic, run `--install` again from the new script. For UI-only updates, replace the script and restart FanCurve. To remove the helper, sudoers rule, launcher, and login item, run:

```sh
uv run --no-project fan_curve.py --uninstall --lang en
```

Uninstall requires administrator authorization and leaves the original script and `.fancurve.json` configuration intact.

## Failure behavior and diagnostics

FanCurve requests system control when it exits, sleeps, loses valid readings, or loses contact with the helper for 12 seconds. During shutdown, Terminal may ask whether to close because the TUI is still running in that window. macOS `SIGTERM` and `SIGHUP` signals trigger normal cleanup and a request for the helper to restore system fan control. The protected helper retries restoration when needed. A forced `SIGKILL` or hardware failure can prevent confirmation, so restoration cannot be guaranteed in every failure mode. After waking, an installed protected session tries once to resume control; press `a` if that attempt fails. This shutdown behavior is described from the signal handling path; real shutdown and hardware restoration have not been verified.

```sh
uv run --no-project fan_curve.py --probe --lang en     # Read-only hardware JSON
uv run --no-project fan_curve.py --simulate --lang en  # Print a simulated curve
```

Direct TUI settings live beside the script in `.fancurve.json`; the packaged app uses `~/Library/Application Support/FanCurve/settings.json`. FanCurve implements the SMC interface independently with Python `ctypes`, with [SMCKit](https://github.com/beltex/SMCKit) as an ABI reference. It only supports Intel Macs running macOS, with `/usr/bin/python3` 3.9 or newer and Command Line Tools.

Run the tests with:

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-project python3 -m unittest discover -s tests -v
```

Tests cover curve logic, simulated hardware, UI layout, both languages, and protected-install file handling in temporary directories. The demo and read-only tests do not prove that a particular Mac accepts fan-speed writes.

[MIT License](LICENSE)
