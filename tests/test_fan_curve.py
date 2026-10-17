import math
import itertools
import os
import plistlib
import re
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fan_curve as fc


def sample(cpu=50.0, hottest=None, fans=None, sensors=None):
    sensors = sensors if sensors is not None else {"TC1C": cpu, "TC2C": cpu}
    hottest = cpu if hottest is None else hottest
    fans = fans if fans is not None else [
        fc.Fan(0, 1000, 2000, 1200, 1200),
        fc.Fan(1, 1500, 2500, 1700, 1700),
    ]
    return fc.Sample(sensors, "fake", hottest, fans)


class LoginAgentTests(unittest.TestCase):
    def test_install_generates_login_agent_and_executable_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            script = home / "source folder" / "fan_curve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# test script\n", encoding="utf-8")
            fc.install_login_agent(script_path=script, home=home, uv_path="/opt/homebrew/bin/uv")

            launcher = home / "Library/Application Support/FanCurve/启动.command"
            plist_path = home / "Library/LaunchAgents/com.fancurve.login.plist"
            self.assertTrue(launcher.is_file())
            self.assertTrue(os.access(launcher, os.X_OK))
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn(str(script), launcher_text)
            self.assertIn("/opt/homebrew/bin/uv", launcher_text)
            self.assertIn("uv run --no-project", launcher_text)
            self.assertIn("--start-hidden", launcher_text)
            self.assertIn("--lang zh", launcher_text)

            with plist_path.open("rb") as stream:
                agent = plistlib.load(stream)
            self.assertEqual(agent["Label"], "com.fancurve.login")
            self.assertTrue(agent["RunAtLoad"])
            self.assertEqual(agent["LimitLoadToSessionType"], "Aqua")
            self.assertEqual(agent["ProgramArguments"], [
                "/usr/bin/open", "-g", "-a", "Terminal", str(launcher.resolve()),
            ])

    def test_reinstall_updates_launcher_script_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first_script = home / "first" / "fan_curve.py"
            second_script = home / "second" / "fan_curve.py"
            for script in (first_script, second_script):
                script.parent.mkdir(parents=True)
                script.write_text("# test script\n", encoding="utf-8")
            fc.install_login_agent(script_path=first_script, home=home, uv_path="/tools/uv")
            fc.install_login_agent(script_path=second_script, home=home, uv_path="/tools/uv")

            launcher = home / "Library/Application Support/FanCurve/启动.command"
            self.assertIn(str(second_script), launcher.read_text(encoding="utf-8"))
            self.assertNotIn(str(first_script), launcher.read_text(encoding="utf-8"))

    def test_english_install_persists_language_for_login(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            script = home / "fan_curve.py"
            script.write_text("# test script\n", encoding="utf-8")
            fc.install_login_agent(script_path=script, home=home, uv_path="/tools/uv", language="en")
            launcher = home / "Library/Application Support/FanCurve/启动.command"
            self.assertIn("--lang en --start-hidden", launcher.read_text(encoding="utf-8"))

    def test_uninstall_removes_only_owned_files_and_preserves_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            script = home / "source" / "fan_curve.py"
            script.parent.mkdir(parents=True)
            script.write_text("# test script\n", encoding="utf-8")
            fc.install_login_agent(script_path=script, home=home, uv_path="/tools/uv")
            launcher = home / "Library/Application Support/FanCurve/启动.command"
            plist_path = home / "Library/LaunchAgents/com.fancurve.login.plist"
            config = script.with_name(".fancurve.json")
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text('{"start": 51, "full": 81, "window": 7}\n', encoding="utf-8")
            unrelated = launcher.parent / "notes.txt"
            unrelated.write_text("keep\n", encoding="utf-8")
            other_agent = plist_path.parent / "com.example.other.plist"
            other_agent.write_text("keep\n", encoding="utf-8")

            fc.uninstall_login_agent(home=home)

            self.assertFalse(launcher.exists())
            self.assertFalse(plist_path.exists())
            self.assertTrue(config.exists())
            self.assertTrue(script.parent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(other_agent.exists())


class ProtectedInstallTests(unittest.TestCase):
    @staticmethod
    def _check_test_directory(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise fc.FanError(f"系统目录权限不安全，未安装：{path}")

    def _temporary_protected_paths(self, directory):
        root = Path(directory)
        system_tools = root / "Library/PrivilegedHelperTools"
        system_tools.mkdir(parents=True)
        sudoers_dir = root / "private/etc/sudoers.d"
        sudoers_dir.mkdir(parents=True)
        helper_dir = system_tools / "FanCurve"
        root_script = helper_dir / "fan_curve.py"
        root_launcher = system_tools / "com.fancurve.helper"
        sudoers_file = sudoers_dir / "fancurve"
        return (patch.multiple(fc, ROOT_HELPER_DIR=helper_dir, ROOT_SCRIPT=root_script,
                               ROOT_LAUNCHER=root_launcher, SUDOERS_FILE=sudoers_file),
                helper_dir, root_script, root_launcher, sudoers_file)

    def test_builds_narrow_protected_policy_and_fixed_system_python_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path_patch, helper_dir, root_script, launcher, sudoers = self._temporary_protected_paths(directory)
            with path_patch, patch.object(fc, "_require_root_installer", return_value="test-user"), \
                 patch.object(fc, "_require_protected_directory", side_effect=self._check_test_directory), \
                 patch.object(fc, "_sudoers_directory_enabled", return_value=True), \
                 patch.object(fc.subprocess, "run", return_value=type("Result", (), {"returncode": 0,
                                      "stderr": "", "stdout": ""})()) as run:
                fc.install_privileged_helper()

            self.assertTrue(helper_dir.is_dir())
            policy = sudoers.read_text(encoding="ascii")
            self.assertEqual(policy, f"{fc.SUDOERS_MARKER}\ntest-user ALL=(root) NOPASSWD: {launcher} \"\"\n")
            self.assertIn("NOPASSWD", policy)
            self.assertEqual(launcher.read_text(),
                             "#!/bin/sh\n" + fc.ROOT_LAUNCHER_MARKER + "\n" +
                             f"exec /usr/bin/python3 -I -u {root_script} --helper\n")
            self.assertEqual(run.call_count, 2)

    def test_uninstall_removes_only_owned_protected_files_and_preserves_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path_patch, helper_dir, root_script, launcher, sudoers = self._temporary_protected_paths(directory)
            with path_patch, patch.object(fc, "_require_root_installer", return_value="test-user"), \
                 patch.object(fc, "_require_protected_directory", side_effect=self._check_test_directory), \
                 patch.object(fc, "_sudoers_directory_enabled", return_value=True), \
                 patch.object(fc.subprocess, "run", return_value=type("Result", (), {"returncode": 0,
                                      "stderr": "", "stdout": ""})()):
                fc.install_privileged_helper()
                config = Path(directory) / "user/.fancurve.json"
                config.parent.mkdir()
                config.write_text('{"start": 51, "full": 81, "window": 7}\n', encoding="utf-8")
                unrelated = helper_dir / "keep.txt"
                unrelated.write_text("keep\n", encoding="utf-8")
                fc.uninstall_privileged_helper()

            self.assertFalse(root_script.exists())
            self.assertFalse(launcher.exists())
            self.assertFalse(sudoers.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(helper_dir.is_dir())
            self.assertTrue(config.exists())

    def test_install_does_not_replace_unrecognized_protected_script(self):
        with tempfile.TemporaryDirectory() as directory:
            path_patch, helper_dir, root_script, _, _ = self._temporary_protected_paths(directory)
            helper_dir.mkdir()
            root_script.write_text("unrelated program\n", encoding="utf-8")
            with path_patch, patch.object(fc, "_require_root_installer", return_value="test-user"), \
                 patch.object(fc, "_require_protected_directory", side_effect=self._check_test_directory), \
                 patch.object(fc, "_sudoers_directory_enabled", return_value=True):
                with self.assertRaisesRegex(fc.FanError, "未覆盖"):
                    fc.install_privileged_helper()
            self.assertEqual(root_script.read_text(encoding="utf-8"), "unrelated program\n")

    def test_install_creates_missing_protected_system_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            path_patch, helper_dir, root_script, launcher, sudoers = self._temporary_protected_paths(directory)
            launcher.parent.rmdir()
            sudoers.parent.rmdir()
            with path_patch, patch.object(fc, "_require_root_installer", return_value="test-user"), \
                 patch.object(fc, "_require_protected_directory", side_effect=self._check_test_directory), \
                 patch.object(fc, "_sudoers_directory_enabled", return_value=True), \
                 patch.object(fc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="")):
                fc.install_privileged_helper()

            self.assertTrue(root_script.is_file())
            self.assertTrue(launcher.is_file())
            self.assertTrue(sudoers.is_file())
            for path in (launcher.parent, sudoers.parent, helper_dir):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_install_rejects_missing_directory_under_writable_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path_patch, _, _, launcher, _ = self._temporary_protected_paths(directory)
            launcher.parent.rmdir()
            launcher.parent.parent.chmod(0o777)
            with path_patch, patch.object(fc, "_require_root_installer", return_value="test-user"), \
                 patch.object(fc, "_require_protected_directory", side_effect=self._check_test_directory), \
                 patch.object(fc, "_sudoers_directory_enabled", return_value=True):
                with self.assertRaisesRegex(fc.FanError, "权限不安全"):
                    fc.install_privileged_helper()
            self.assertFalse(launcher.parent.exists())

    def test_system_python_unavailable_is_not_reported_as_old_version(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = Path(directory) / "python3"
            interpreter.touch()
            with patch.object(fc.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="xcode-select: no developer tools")):
                with self.assertRaisesRegex(fc.FanError, "命令行工具"):
                    fc._require_system_python(interpreter)

    def test_system_python_version_is_checked_after_successful_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            interpreter = Path(directory) / "python3"
            interpreter.touch()
            with patch.object(fc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="3.8\n", stderr="")):
                with self.assertRaisesRegex(fc.FanError, "版本过旧"):
                    fc._require_system_python(interpreter)
            with patch.object(fc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="3.9\n", stderr="")):
                fc._require_system_python(interpreter)

    def test_installed_helper_client_invokes_sudo_without_extra_arguments(self):
        with patch.object(fc.subprocess, "Popen") as popen, \
             patch.object(fc.HelperClient, "read_response", return_value={"ok": True}):
            fc.HelperClient(installed=True)

        self.assertEqual(popen.call_args.args[0], ["/usr/bin/sudo", "-n", str(fc.ROOT_LAUNCHER)])


class ProtectedSleepResumeTests(unittest.TestCase):
    def test_sleep_response_resets_auto_enable_latch_only_with_protected_helper(self):
        response = {"active": False, "owns_fans": False, "ok": True,
                    "message": "电脑进入休眠，自动调速已停用。"}
        with patch.object(fc, "protected_helper_available", return_value=True):
            ui = fc.TerminalUI(demo=False)
            ui.active = True
            ui.auto_enable_attempted = True
            ui.accept(response)
            self.assertFalse(ui.auto_enable_attempted)

        with patch.object(fc, "protected_helper_available", return_value=False):
            ui = fc.TerminalUI(demo=False)
            ui.active = True
            ui.auto_enable_attempted = True
            ui.accept(response)
            self.assertTrue(ui.auto_enable_attempted)


class ConfigAndCodecTests(unittest.TestCase):
    def test_configuration_boundaries(self):
        for config in (
            fc.Config(start=35, full=55, window=2),
            fc.Config(start=70, full=90, window=20),
        ):
            config.validate()

    def test_configuration_rejects_nonfinite_and_invalid_ranges(self):
        for config in (
            fc.Config(start=math.nan),
            fc.Config(full=math.inf),
            fc.Config(window=math.nan),
            fc.Config(start=34),
            fc.Config(full=91),
            fc.Config(start=60, full=69),
            fc.Config(window=1.99),
            fc.Config(window=20.01),
        ):
            with self.subTest(config=config), self.assertRaises(fc.FanError):
                config.validate()

    def test_decodes_supported_smc_numeric_formats(self):
        cases = (
            ("sp78", struct.pack(">h", -384), -1.5),
            ("fpe2", struct.pack(">H", 4000), 1000.0),
            ("flt ", struct.pack("<f", 12.5), 12.5),
            ("ui8 ", struct.pack("B", 8), 8.0),
            ("ui16", struct.pack(">H", 513), 513.0),
            ("ui32", struct.pack(">I", 65537), 65537.0),
        )
        for kind, raw, expected in cases:
            with self.subTest(kind=kind):
                self.assertAlmostEqual(fc.decode_number(kind, raw), expected)

    def test_decoder_rejects_nan_unknown_types_and_wrong_size(self):
        for kind, raw in (("flt ", struct.pack("<f", math.nan)), ("sp78", b"\0")):
            with self.subTest(kind=kind), self.assertRaises(fc.FanError):
                fc.decode_number(kind, raw)
        with self.assertRaises(fc.FanError):
            fc.decode_number("xxxx", b"\0\0")

    def test_encoder_checks_sign_range_and_integer_types(self):
        self.assertAlmostEqual(fc.decode_number("fpe2", fc.encode_number("fpe2", 825.0)), 825.0)
        self.assertAlmostEqual(fc.decode_number("flt ", fc.encode_number("flt ", 42.25)), 42.25)
        for kind, value in (("fpe2", -1), ("fpe2", 16384), ("ui8 ", 256),
                            ("ui8 ", 1.5), ("ui16", 65536), ("flt ", -0.1),
                            ("flt ", math.nan)):
            with self.subTest(kind=kind, value=value), self.assertRaises(fc.FanError):
                fc.encode_number(kind, value)


class ControllerTests(unittest.TestCase):
    def test_maps_cpu_average_to_each_fans_own_limits(self):
        controller = fc.Controller()
        result = controller.update(sample(cpu=65, sensors={"a": 60, "b": 70}), fc.Config(window=2), 0)
        self.assertEqual(result.smoothed, 65)
        self.assertEqual(result.targets, [1500, 2000])

    def test_six_second_window_discards_old_values(self):
        controller = fc.Controller()
        config = fc.Config(window=6)
        controller.update(sample(cpu=40), config, 0)
        controller.update(sample(cpu=60), config, 2)
        controller.update(sample(cpu=80), config, 4)
        result = controller.update(sample(cpu=70), config, 6)
        self.assertAlmostEqual(result.smoothed, 70)

    def test_deadband_suppresses_small_changes_but_endpoint_reaches_max(self):
        controller = fc.Controller()
        config = fc.Config(window=2)
        initial = controller.update(sample(cpu=50), config, 0)
        small_change = controller.update(sample(cpu=51), config, 2)
        self.assertEqual(initial.targets, small_change.targets)
        endpoint = controller.update(sample(cpu=80), config, 4)
        self.assertEqual(endpoint.targets, [2000, 2500])

    def test_hotspot_protection_latches_until_below_85(self):
        controller = fc.Controller()
        config = fc.Config(window=2)
        hot = controller.update(sample(cpu=60, hottest=90), config, 0)
        held = controller.update(sample(cpu=60, hottest=87), config, 2)
        released = controller.update(sample(cpu=60, hottest=84), config, 4)
        self.assertTrue(hot.emergency)
        self.assertEqual(hot.targets, [2000, 2500])
        self.assertTrue(held.emergency)
        self.assertFalse(released.emergency)

    def test_gap_over_five_seconds_discards_previous_samples(self):
        controller = fc.Controller()
        config = fc.Config(window=20)
        controller.update(sample(cpu=40), config, 0)
        result = controller.update(sample(cpu=80), config, 5.1)
        self.assertEqual(result.smoothed, 80)

    def test_rejects_missing_and_implausible_sensor_data(self):
        for bad in (
            fc.Sample({}, "empty", 50, [fc.Fan(0, 1000, 2000, 1200, 1200)]),
            sample(sensors={"TC1C": 129}),
        ):
            with self.subTest(bad=bad), self.assertRaises(fc.FanError):
                fc.Controller().update(bad, fc.Config(), 0)


class FakeHardware:
    def __init__(self):
        self.owns_fans = False
        self.current = sample()
        self.applied = []
        self.restore_count = 0
        self.fail_apply = False
        self.fail_restore = 0
        self.fail_sample = False
        self.foreign = False

    def sample(self):
        if self.fail_sample:
            raise fc.FanError("温度读取失败")
        return self.current

    def preflight(self):
        if self.foreign:
            raise fc.FanError("其他程序已接管")

    def apply(self, targets):
        self.applied.append(targets)
        self.owns_fans = True
        if self.fail_apply:
            raise fc.FanError("部分写入失败")

    def restore(self):
        self.restore_count += 1
        if self.fail_restore:
            self.fail_restore -= 1
            raise fc.FanError("恢复失败")
        self.owns_fans = False


class SessionTests(unittest.TestCase):
    def enable(self, session, now=0):
        return session.handle({"command": "enable", "config": {"start": 50, "full": 80, "window": 6}}, now)

    def test_enable_applies_immediately(self):
        hardware = FakeHardware()
        session = fc.Session(hardware, 0)
        response = self.enable(session)
        self.assertTrue(response["active"])
        self.assertEqual(len(hardware.applied), 1)

    def test_status_does_not_refresh_heartbeat_but_heartbeat_does(self):
        hardware = FakeHardware()
        session = fc.Session(hardware, 0)
        self.enable(session)
        session.handle({"command": "status"}, 10)
        session.tick(12.1)
        self.assertTrue(session.exit_requested)
        self.assertFalse(hardware.owns_fans)

        hardware = FakeHardware()
        session = fc.Session(hardware, 0)
        self.enable(session)
        session.handle({"command": "heartbeat"}, 10)
        session.tick(12.1)
        self.assertFalse(session.exit_requested)
        self.assertTrue(hardware.owns_fans)

    def test_timeout_restores_and_allows_exit(self):
        hardware = FakeHardware()
        session = fc.Session(hardware, 0)
        self.enable(session)
        session.tick(12.01)
        self.assertGreaterEqual(hardware.restore_count, 1)
        self.assertTrue(session.can_exit)

    def test_partial_apply_failure_attempts_restore(self):
        hardware = FakeHardware()
        hardware.fail_apply = True
        session = fc.Session(hardware, 0)
        response = self.enable(session)
        self.assertFalse(response["active"])
        self.assertEqual(hardware.restore_count, 1)
        self.assertFalse(hardware.owns_fans)

    def test_restore_failure_retries_and_blocks_exit(self):
        hardware = FakeHardware()
        hardware.fail_restore = 1
        session = fc.Session(hardware, 0)
        self.enable(session)
        session.handle({"command": "shutdown"}, 1)
        self.assertFalse(session.can_exit)
        session.tick(2)
        self.assertEqual(hardware.restore_count, 2)
        self.assertTrue(session.can_exit)

    def test_temperature_failure_restores_owned_fans(self):
        hardware = FakeHardware()
        session = fc.Session(hardware, 0)
        self.enable(session)
        hardware.fail_sample = True
        session.tick(2)
        self.assertFalse(hardware.owns_fans)
        self.assertGreaterEqual(hardware.restore_count, 1)

    def test_foreign_preflight_does_not_take_or_restore_control(self):
        hardware = FakeHardware()
        hardware.foreign = True
        session = fc.Session(hardware, 0)
        response = self.enable(session)
        self.assertFalse(response["active"])
        self.assertEqual(hardware.applied, [])
        self.assertEqual(hardware.restore_count, 0)


class TerminalUITests(unittest.TestCase):
    def test_english_demo_dashboard_help_and_settings_have_no_chinese(self):
        class FakeScreen:
            def __init__(self):
                self.cells = []

            def getmaxyx(self):
                return 30, 110

            def erase(self):
                self.cells.clear()

            def addstr(self, row, col, text, style=0):
                self.cells.append(text)

            def refresh(self):
                pass

        ui = fc.TerminalUI(demo=True)
        ui.sample = fc.DemoHardware().sample()
        ui.smoothed = 69.5
        ui.active = True
        ui.message = "正在按 CPU 平均温度调速"
        ui.history.append(69.5)
        screen = FakeScreen()
        with patch.object(fc, "LANGUAGE", "en"), patch("fan_curve.curses.color_pair", return_value=0):
            for mode in ("dashboard", "help", "settings"):
                ui.help = mode == "help"
                ui.edit = fc.Config() if mode == "settings" else None
                ui.draw(screen)
                visible = "\n".join(screen.cells)
                self.assertIsNone(re.search(r"[\u4e00-\u9fff]", visible), mode)
                self.assertIn("FanCurve  /  CPU temperature & fans", visible)
                if mode == "dashboard":
                    self.assertIn("Automatic fan control on", visible)
                    self.assertIn("Demo: 8 simulated cores", visible)
                elif mode == "help":
                    self.assertIn("Keyboard shortcuts", visible)
                else:
                    self.assertIn("Settings  /", visible)

    def test_english_dynamic_diagnostics_and_sensor_source(self):
        with patch.object(fc, "LANGUAGE", "en"):
            self.assertEqual(fc.translate_text("8 个 CPU 核心传感器的平均值"),
                             "Average of 8 CPU core sensors")
            self.assertEqual(fc.translate_text("CPU 封装温度 TC0P（无核心传感器）"),
                             "CPU package temperature TC0P (no core sensors)")
            self.assertEqual(fc.translate_text("风扇 2"), "Fan 2")
            self.assertEqual(fc.translate_text("原因：没有有效的 CPU 温度或风扇数据。"),
                             "Reason: No valid CPU temperature or fan data.")
            diagnostic = ("SMC 未确认写入，自动调速已停止。"
                          "风扇1：目标3240→回读1800RPM，模式1（手动）；"
                          "风扇2：目标3000→回读2200RPM，模式1（手动）")
            translated = fc.translate_text(diagnostic)
            self.assertIn("Fan 1: target 3240, readback 1800 RPM", translated)
            self.assertIn("Fan 2: target 3000, readback 2200 RPM", translated)
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", translated))

    def test_terminal_window_action_targets_only_the_current_tty(self):
        completed = fc.subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        with patch.object(fc.os, "ttyname", return_value="/dev/ttys123"), \
             patch.object(fc.subprocess, "run", return_value=completed) as run:
            fc.set_terminal_window_visible(False)
            self.assertEqual(run.call_args.args[0][-2:], ["/dev/ttys123", "hide"])
            fc.set_terminal_window_visible(True)
            self.assertEqual(run.call_args.args[0][-2:], ["/dev/ttys123", "show"])
            run.return_value = fc.subprocess.CompletedProcess([], 0, stdout="missing\n", stderr="")
            with self.assertRaisesRegex(fc.FanError, "无法定位"):
                fc.set_terminal_window_visible(False)

    def test_menu_bar_click_signal_is_consumed_once(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(reader, False)
            client = object.__new__(fc.MenuBarClient)
            client.process = SimpleNamespace(stdout=os.fdopen(reader, "rb", buffering=0))
            client.pending_actions = b""
            os.write(writer, b"SHOW\n")
            self.assertTrue(client.show_requested())
            self.assertFalse(client.show_requested())
        finally:
            client.process.stdout.close()
            os.close(writer)

    def test_menu_bar_temperature_uses_same_smoothed_reading_format(self):
        self.assertEqual(fc.menu_temperature(69.54), "69.5°C")
        self.assertEqual(fc.menu_temperature(None), "--.-°C")
        self.assertEqual(fc.menu_temperature(float("nan")), "--.-°C")

    def test_auto_enable_waits_for_valid_sample_and_attempts_only_once(self):
        ui = fc.TerminalUI(demo=True)
        screen = object()
        with patch.object(ui, "enable") as enable:
            ui.maybe_auto_enable(screen)
            self.assertFalse(ui.auto_enable_attempted)
            enable.assert_not_called()

            ui.sample = sample(cpu=55)
            ui.maybe_auto_enable(screen)
            ui.maybe_auto_enable(screen)

        self.assertTrue(ui.auto_enable_attempted)
        enable.assert_called_once_with(screen)

    def test_monitor_mode_never_auto_enables_but_keeps_manual_enable_available(self):
        ui = fc.TerminalUI(demo=True, monitor=True)
        ui.sample = sample(cpu=55)
        screen = object()
        with patch.object(ui, "enable") as enable:
            ui.maybe_auto_enable(screen)

        self.assertFalse(ui.auto_enable_attempted)
        enable.assert_not_called()

    def test_auto_enable_failure_is_not_retried(self):
        ui = fc.TerminalUI(demo=True)
        ui.sample = sample(cpu=55)
        screen = object()
        with patch.object(ui, "enable", side_effect=RuntimeError("expected test failure")) as enable:
            with self.assertRaisesRegex(RuntimeError, "expected test failure"):
                ui.maybe_auto_enable(screen)
            ui.maybe_auto_enable(screen)

        enable.assert_called_once_with(screen)

    def test_large_temperature_renders_value_and_missing_placeholder(self):
        self.assertEqual(
            fc.large_temperature(69.5),
            (
                "████ ████    ████",
                "█    █  █    █   ",
                "████ ████    ████",
                "█  █    █ ██    █",
                "████ ████ ██ ████",
            ),
        )
        placeholder = fc.large_temperature(None)
        self.assertEqual(len(placeholder), 5)
        self.assertEqual(placeholder, fc.large_temperature(math.nan))
        self.assertEqual(placeholder[2], "████ ████    ████")

    def test_78_by_24_layout_keeps_right_status_separate_from_fan_table(self):
        class FakeScreen:
            def __init__(self):
                self.cells = []

            def getmaxyx(self):
                return 24, 78

            def erase(self):
                pass

            def addstr(self, row, col, text, style=0):
                self.cells.append((row, col, text))

            def refresh(self):
                pass

        ui = fc.TerminalUI(demo=True)
        ui.sample = sample(cpu=69.5)
        ui.smoothed = 69.5
        screen = FakeScreen()
        with patch("fan_curve.curses.color_pair", return_value=0):
            ui.draw(screen)
        expected_digit_rows = fc.large_temperature(69.5)
        for row, text in enumerate(expected_digit_rows, 1):
            self.assertIn((row, 2, text), screen.cells)
        self.assertIn((5, 28, "°C"), screen.cells)
        self.assertTrue(any(row == 1 and col == 32 for row, col, _ in screen.cells))
        self.assertTrue(any(row == 9 and "当前目标" in text for row, _, text in screen.cells))
        self.assertTrue(any(row == 10 and "风扇 1" in text for row, _, text in screen.cells))
        self.assertFalse(any("至少 78 列" in text for _, _, text in screen.cells))

    def test_wrap_keeps_two_fan_write_diagnostics_in_three_lines(self):
        diagnostic = (
            "原因：SMC 未确认写入，自动调速已停止。"
            "风扇1：目标3240→回读1800RPM，模式1（手动）；"
            "风扇2：目标3000→回读2200RPM，模式1（手动）"
        )
        lines = fc._wrap(diagnostic, 44, 3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith("。"))
        self.assertTrue(lines[1].endswith("；"))
        self.assertIn("风扇2：目标3000→回读2200RPM", lines[2])
        self.assertFalse(any(line.endswith("…") for line in lines))
        self.assertEqual("".join(lines), diagnostic)

    def test_monitor_read_failure_is_not_reported_as_enable_failure(self):
        class BrokenHardware:
            owns_fans = False

            def sample(self):
                raise fc.FanError("AppleSMC 读取失败")

            def close(self):
                pass

        ui = fc.TerminalUI(demo=True)
        ui.hardware = BrokenHardware()
        ui.refresh(0)
        banner = ui.status_banner()
        self.assertEqual(banner[0], "读取失败 · 曲线未生效")
        self.assertIn("AppleSMC 读取失败", banner[2])
        self.assertNotIn("启用失败", banner[0])

    def test_enable_failure_banner_survives_normal_monitor_refresh(self):
        ui = fc.TerminalUI(demo=True)
        ui.accept({"ok": False, "active": False, "owns_fans": False,
                   "message": "SMC 未确认写入：风扇1目标2400回读1800 RPM"})
        before = ui.status_banner()
        ui.refresh(0)
        after = ui.status_banner()
        self.assertEqual(before[3], "error")
        self.assertEqual(after[0], "启用失败 · 曲线未生效")
        self.assertIn("SMC 未确认写入", after[2])
        self.assertEqual(after[3], "error")

    def test_invalid_settings_error_banner_survives_monitor_refresh(self):
        ui = fc.TerminalUI(demo=True)
        ui.refresh(0)
        ui.edit = fc.Config(start=34)
        ui.edit_key(10)
        banner = ui.status_banner()
        self.assertEqual(banner[0], "设置未保存 · 曲线未生效")
        self.assertIn("设置错误", banner[2])
        self.assertNotIn("启用失败", banner[0])
        ui.refresh(2)
        after_refresh = ui.status_banner()
        self.assertEqual(after_refresh[0], "设置未保存 · 曲线未生效")
        self.assertIn("设置错误", after_refresh[2])


class TypedFakeSMC:
    def __init__(self, target_type="flt ", ignore_auto_target=False,
                 fail_target_once_after_manual=False, legacy=False, gpu_initial=None,
                 stale_target_reads=0, never_confirm_target=False):
        self.kinds = {"F0Tg": target_type}
        self.values = {
            "FNum": 1, "TC1C": 55.0, "F0Mn": 1000.0, "F0Mx": 3000.0,
            "F0Ac": 1800.0, "F0Tg": 1800.0,
        }
        if legacy:
            self.values["FS! "] = 0x80
        else:
            self.values["F0Md"] = 0
        if gpu_initial is not None:
            self.values["TGDD"] = gpu_initial
        self.closed = False
        self.fail_cpu = False
        self.ignore_auto_target = ignore_auto_target
        self.fail_target_once_after_manual = fail_target_once_after_manual
        self.stale_target_reads = stale_target_reads
        self.never_confirm_target = never_confirm_target
        self.pending_target = None

    def read(self, key):
        if key == "TC1C" and self.fail_cpu:
            raise fc.FanError("CPU sensor offline")
        if key.startswith("TC") or key.startswith("TG"):
            if key in self.values:
                return self.values[key]
            raise fc.FanError("unknown sensor")
        if key not in self.values:
            raise fc.FanError("unknown key")
        value = self.values[key]
        if key == "F0Tg":
            if self.pending_target is not None:
                if self.never_confirm_target:
                    pass
                elif self.stale_target_reads > 0:
                    self.stale_target_reads -= 1
                else:
                    self.values[key] = self.pending_target
                    self.pending_target = None
                    value = self.values[key]
            raw = fc.encode_number(self.kinds[key], value)
            return fc.decode_number(self.kinds[key], raw)
        return value

    def write(self, key, value):
        if key == "F0Tg":
            manual = (self.values.get("F0Md", 0) == 1 or
                      bool(self.values.get("FS! ", 0) & 1))
            if self.ignore_auto_target and not manual:
                return
            if self.fail_target_once_after_manual and manual:
                self.fail_target_once_after_manual = False
                raise fc.FanError("模拟目标写入失败")
            raw = fc.encode_number(self.kinds[key], value)
            encoded_value = fc.decode_number(self.kinds[key], raw)
            if self.stale_target_reads or self.never_confirm_target:
                self.pending_target = encoded_value
            else:
                self.values[key] = encoded_value
        else:
            self.values[key] = value

    def close(self):
        self.closed = True


class HardwareTests(unittest.TestCase):
    def test_zero_gpu_reading_is_retained_for_later_overtemperature_guard(self):
        smc = TypedFakeSMC(gpu_initial=0)
        hardware = fc.Hardware(smc)
        smc.values["TGDD"] = 95
        current = hardware.sample()
        self.assertEqual(current.hottest, 95)
        decision = fc.Controller().update(current, fc.Config(), 0)
        self.assertTrue(decision.emergency)
        self.assertEqual(decision.targets, [3000])

    def test_float_and_fpe2_targets_work_through_fake_smc_interface(self):
        for kind in ("flt ", "fpe2"):
            with self.subTest(kind=kind):
                smc = TypedFakeSMC(kind)
                hardware = fc.Hardware(smc)
                hardware.apply([2400])
                self.assertEqual(smc.read("F0Tg"), 2400)

    def test_apply_switches_to_manual_before_setting_target(self):
        smc = TypedFakeSMC(ignore_auto_target=True)
        hardware = fc.Hardware(smc)
        hardware.apply([2400])
        self.assertEqual(smc.read("F0Md"), 1)
        self.assertEqual(smc.read("F0Tg"), 2400)

    def test_partial_hardware_write_keeps_ownership_until_restore(self):
        smc = TypedFakeSMC(fail_target_once_after_manual=True)
        hardware = fc.Hardware(smc)
        with self.assertRaises(fc.FanError):
            hardware.apply([2400])
        self.assertTrue(hardware.owns_fans)
        self.assertEqual(smc.read("F0Md"), 1)
        hardware.restore()
        self.assertEqual(smc.read("F0Md"), 0)
        self.assertFalse(hardware.owns_fans)

    def test_apply_retries_stale_readback_until_firmware_updates(self):
        smc = TypedFakeSMC(stale_target_reads=3)
        hardware = fc.Hardware(smc)
        clock = itertools.count()
        with patch("fan_curve.time.monotonic", side_effect=lambda: next(clock) / 100), \
             patch("fan_curve.time.sleep"):
            hardware.apply([2400])
        self.assertEqual(smc.read("F0Md"), 1)
        self.assertEqual(smc.read("F0Tg"), 2400)
        self.assertTrue(hardware.owns_fans)

    def test_unconfirmed_readback_reports_values_and_can_be_restored(self):
        smc = TypedFakeSMC(never_confirm_target=True)
        hardware = fc.Hardware(smc)
        clock = itertools.count()
        with patch("fan_curve.time.monotonic", side_effect=lambda: next(clock) / 5), \
             patch("fan_curve.time.sleep"):
            with self.assertRaisesRegex(fc.FanError, r"风扇1.*目标2400.*回读1800.*模式1"):
                hardware.apply([2400])
        self.assertTrue(hardware.owns_fans)
        hardware.restore()
        self.assertEqual(smc.read("F0Md"), 0)
        self.assertFalse(hardware.owns_fans)

    def test_legacy_mode_mask_preserves_unrelated_bits(self):
        smc = TypedFakeSMC(legacy=True)
        hardware = fc.Hardware(smc)
        hardware.apply([2400])
        self.assertEqual(smc.read("FS! "), 0x81)
        self.assertEqual(smc.read("F0Tg"), 2400)
        hardware.restore()
        self.assertEqual(smc.read("FS! "), 0x80)

    def test_targets_must_fit_each_fans_reported_bounds(self):
        hardware = fc.Hardware(TypedFakeSMC())
        for target in (999, 3001):
            with self.subTest(target=target), self.assertRaises(fc.FanError):
                hardware.apply([target])

    def test_restore_does_not_read_cpu_temperature(self):
        smc = TypedFakeSMC()
        hardware = fc.Hardware(smc)
        hardware.apply([2200])
        smc.fail_cpu = True
        hardware.restore()
        self.assertFalse(hardware.owns_fans)
        self.assertEqual(smc.read("F0Md"), 0)


if __name__ == "__main__":
    unittest.main()
