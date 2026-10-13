import math
import struct
import unittest

import fan_curve as fc


def sample(cpu=50.0, hottest=None, fans=None, sensors=None):
    sensors = sensors if sensors is not None else {"TC1C": cpu, "TC2C": cpu}
    hottest = cpu if hottest is None else hottest
    fans = fans if fans is not None else [
        fc.Fan(0, 1000, 2000, 1200, 1200),
        fc.Fan(1, 1500, 2500, 1700, 1700),
    ]
    return fc.Sample(sensors, "fake", hottest, fans)


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


class TypedFakeSMC:
    def __init__(self, target_type="flt ", ignore_auto_target=False,
                 fail_target_once_after_manual=False, legacy=False, gpu_initial=None):
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
            self.values[key] = fc.decode_number(self.kinds[key], raw)
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
