#!/usr/bin/env python3
"""Intel macOS fan controller. Standard library only; no build step.

SMC ABI reference: beltex/SMCKit. Implemented independently using ctypes.
Default invocation is read-only until the user explicitly enables control.
"""
from __future__ import annotations

import argparse
import ctypes as C
import curses
import dataclasses
import fcntl
import json
import locale
import math
import os
from pathlib import Path
import platform
import select
import signal
import struct
import subprocess
import sys
import time
import unicodedata
from collections import deque
from typing import Any

SAMPLE_INTERVAL = 2.0
HEARTBEAT_TIMEOUT = 12.0
SETTINGS_PATH = Path(__file__).resolve().with_name(".fancurve.json")


class FanError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Config:
    start: float = 50.0
    full: float = 80.0
    window: float = 6.0

    def validate(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (self.start, self.full, self.window)):
            raise FanError("配置必须是有限数值。")
        if not (35 <= self.start <= 70 and 55 <= self.full <= 90 and
                self.full - self.start >= 10 and 2 <= self.window <= 20):
            raise FanError("起点 35–70°C，满速 55–90°C，至少相差 10°C，平滑 2–20 秒。")

    def fraction(self, temperature: float) -> float:
        return min(1.0, max(0.0, (temperature - self.start) / (self.full - self.start)))

    @classmethod
    def from_dict(cls, data: Any) -> Config:
        if not isinstance(data, dict) or set(data) != {"start", "full", "window"}:
            raise FanError("配置格式不正确。")
        result = cls(**data)
        result.validate()
        return result


@dataclasses.dataclass
class Fan:
    id: int
    minimum: float
    maximum: float
    actual: float
    target: float
    mode: int = 0


@dataclasses.dataclass
class Sample:
    sensors: dict[str, float]
    source: str
    hottest: float
    fans: list[Fan]

    @property
    def average(self) -> float:
        return sum(self.sensors.values()) / len(self.sensors)

    def validate(self) -> None:
        if not self.sensors or not self.fans:
            raise FanError("没有有效的 CPU 温度或风扇数据。")
        if any(not math.isfinite(v) or not 1 <= v <= 115
               for v in [*self.sensors.values(), self.hottest]):
            raise FanError("温度读取异常，自动调速已停止。")
        for fan in self.fans:
            if (any(not math.isfinite(v) for v in (fan.minimum, fan.maximum, fan.actual, fan.target))
                    or not 100 <= fan.minimum < fan.maximum <= 12000
                    or not 0 <= fan.actual <= 15000 or not 0 <= fan.target <= 15000):
                raise FanError("风扇数据超出有效范围，自动调速已停止。")

    def to_dict(self) -> dict:
        result = dataclasses.asdict(self)
        result["average"] = self.average
        return result

    @classmethod
    def from_dict(cls, data: dict) -> Sample:
        sample = cls(data["sensors"], data["source"], data["hottest"], [Fan(**v) for v in data["fans"]])
        sample.validate()
        return sample


@dataclasses.dataclass
class Decision:
    smoothed: float
    targets: list[float]
    emergency: bool


class Controller:
    def __init__(self) -> None:
        self.history: deque[tuple[float, float]] = deque()
        self.last_targets: list[float] = []
        self.emergency = False

    def reset(self) -> None:
        self.history.clear()
        self.last_targets = []
        self.emergency = False

    def update(self, sample: Sample, config: Config, now: float) -> Decision:
        config.validate()
        sample.validate()
        if self.history and (now < self.history[-1][0] or now - self.history[-1][0] > 5):
            self.reset()
        self.history.append((now, sample.average))
        while self.history and now - self.history[0][0] >= config.window:
            self.history.popleft()
        smoothed = sum(v for _, v in self.history) / len(self.history)
        if sample.hottest >= 90:
            self.emergency = True
        elif sample.hottest < 85:
            self.emergency = False
        fraction = 1.0 if self.emergency else config.fraction(smoothed)
        targets = []
        for i, fan in enumerate(sample.fans):
            desired = round(fan.minimum + (fan.maximum - fan.minimum) * fraction)
            previous = self.last_targets[i] if i < len(self.last_targets) else desired
            target = previous if 0 < fraction < 1 and abs(desired - previous) < 100 else desired
            targets.append(min(fan.maximum, max(fan.minimum, target)))
        self.last_targets = targets
        return Decision(smoothed, targets, self.emergency)


class _Version(C.Structure):
    _fields_ = [("major", C.c_uint8), ("minor", C.c_uint8), ("build", C.c_uint8),
                ("reserved", C.c_uint8), ("release", C.c_uint16)]


class _Limits(C.Structure):
    _fields_ = [("version", C.c_uint16), ("length", C.c_uint16), ("cpu", C.c_uint32),
                ("gpu", C.c_uint32), ("memory", C.c_uint32)]


class _KeyInfo(C.Structure):
    _fields_ = [("size", C.c_uint32), ("type", C.c_uint32), ("attributes", C.c_uint8)]


class _SMCMessage(C.Structure):
    _fields_ = [("key", C.c_uint32), ("version", _Version), ("limits", _Limits),
                ("info", _KeyInfo), ("result", C.c_uint8), ("status", C.c_uint8),
                ("command", C.c_uint8), ("index", C.c_uint32), ("bytes", C.c_uint8 * 32)]


def decode_number(kind: str, data: bytes) -> float:
    formats = {"sp78": (">h", 256), "fpe2": (">H", 4), "flt ": ("<f", 1),
               "ui8 ": ("B", 1), "flag": ("B", 1), "ui16": (">H", 1), "ui32": (">I", 1)}
    if kind not in formats:
        raise FanError(f"不支持 SMC 数据类型 {kind!r}。")
    fmt, scale = formats[kind]
    if len(data) != struct.calcsize(fmt):
        raise FanError("SMC 数据长度不正确。")
    value = struct.unpack(fmt, data)[0] / scale
    if not math.isfinite(value):
        raise FanError("SMC 返回非有限数值。")
    return value


def encode_number(kind: str, value: float) -> bytes:
    if not math.isfinite(value) or value < 0:
        raise FanError("SMC 写入数值不正确。")
    if kind == "fpe2" and value <= 16383:
        return struct.pack(">H", round(value * 4))
    if kind == "flt ":
        return struct.pack("<f", value)
    if kind in ("ui8 ", "flag") and value <= 255 and value == int(value):
        return struct.pack("B", int(value))
    if kind == "ui16" and value <= 65535 and value == int(value):
        return struct.pack(">H", int(value))
    raise FanError(f"不支持向 {kind!r} 写入这个数值。")


def _bind(library: Any, name: str, arguments: list, result: Any) -> Any:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


class SMC:
    def __init__(self) -> None:
        if sys.platform != "darwin" or platform.machine() != "x86_64":
            raise FanError("此版本只支持 Intel Mac 的 macOS。")
        if C.sizeof(_SMCMessage) != 80:
            raise FanError("AppleSMC 接口结构大小不正确。")
        self.lib = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        system = C.CDLL("/usr/lib/libSystem.B.dylib")
        task = C.c_uint32.in_dll(system, "mach_task_self_").value
        matching = _bind(self.lib, "IOServiceMatching", [C.c_char_p], C.c_void_p)
        service_get = _bind(self.lib, "IOServiceGetMatchingService", [C.c_uint32, C.c_void_p], C.c_uint32)
        service = service_get(0, matching(b"AppleSMC"))
        if not service:
            raise FanError("系统没有 AppleSMC 设备。")
        self.connection = C.c_uint32(0)
        open_service = _bind(self.lib, "IOServiceOpen", [C.c_uint32, C.c_uint32, C.c_uint32, C.POINTER(C.c_uint32)], C.c_uint32)
        rc = open_service(service, task, 0, C.byref(self.connection))
        _bind(self.lib, "IOObjectRelease", [C.c_uint32], C.c_uint32)(service)
        if rc:
            raise FanError(f"无法访问 AppleSMC（0x{rc:08x}）。")
        self.call_struct = _bind(self.lib, "IOConnectCallStructMethod", [C.c_uint32, C.c_uint32,
            C.c_void_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_size_t)], C.c_uint32)
        self.close_service = _bind(self.lib, "IOServiceClose", [C.c_uint32], C.c_uint32)
        self.cache: dict[str, tuple[int, str]] = {}

    def close(self) -> None:
        if self.connection.value:
            self.close_service(self.connection.value)
            self.connection.value = 0

    def _call(self, message: _SMCMessage) -> _SMCMessage:
        result = _SMCMessage()
        size = C.c_size_t(C.sizeof(result))
        rc = self.call_struct(self.connection.value, 2, C.byref(message), C.sizeof(message),
                              C.byref(result), C.byref(size))
        if rc or result.result:
            key = message.key.to_bytes(4, "big").decode("ascii", errors="replace")
            raise FanError(f"SMC {key} 操作失败（IO 0x{rc:08x}，SMC {result.result}）。")
        if size.value != 80:
            raise FanError("SMC 返回了不同的接口结构。")
        return result

    def info(self, key: str) -> tuple[int, str]:
        if key not in self.cache:
            if len(key.encode("ascii")) != 4:
                raise FanError("SMC 键名必须为 4 字节。")
            message = _SMCMessage(key=int.from_bytes(key.encode("ascii"), "big"), command=9)
            reply = self._call(message)
            if not 1 <= reply.info.size <= 32:
                raise FanError(f"SMC {key} 数据长度无效。")
            self.cache[key] = (reply.info.size, reply.info.type.to_bytes(4, "big").decode("ascii"))
        return self.cache[key]

    def read(self, key: str) -> float:
        size, kind = self.info(key)
        message = _SMCMessage(key=int.from_bytes(key.encode("ascii"), "big"), command=5)
        message.info.size = size
        reply = self._call(message)
        return decode_number(kind, bytes(reply.bytes[:size]))

    def write(self, key: str, value: float) -> None:
        if os.geteuid() != 0:
            raise FanError("写入风扇需要管理员授权。")
        # Keep the writable surface limited to mode and target keys.
        if key != "FS! " and not (len(key) == 4 and key[0] == "F" and key[1].isdigit()
                                  and key[2:] in ("Md", "Tg")):
            raise FanError("此程序只允许写入风扇目标与模式。")
        size, kind = self.info(key)
        data = encode_number(kind, value)
        if len(data) != size:
            raise FanError("SMC 写入长度不一致。")
        message = _SMCMessage(key=int.from_bytes(key.encode("ascii"), "big"), command=6)
        message.info.size = size
        for i, byte in enumerate(data):
            message.bytes[i] = byte
        self._call(message)


class Hardware:
    def __init__(self, smc: Any = None) -> None:
        self.smc = smc if smc is not None else SMC()
        self.owns_fans = False
        try:
            number = self.smc.read("FNum")
            if not 1 <= number <= 8 or number != int(number):
                raise FanError("没有可控制的风扇。")
            self.count = int(number)
            self.cpu_keys = [f"TC{i:X}C" for i in range(1, 16) if self._temperature(f"TC{i:X}C") is not None]
            self.source = f"{len(self.cpu_keys)} 个 CPU 核心传感器的平均值"
            if not self.cpu_keys:
                package = next((k for k in ("TCXC", "TC0F", "TC0E", "TC0D") if self._temperature(k) is not None), None)
                if package is None:
                    raise FanError("找不到有效的 CPU 温度传感器。")
                self.cpu_keys = [package]
                self.source = f"CPU 封装温度 {package}（无核心传感器）"
            self.guard_keys = [k for k in ("TCXC", "TC0F", "TC0E", "TC0D", "TCGC", "TG0D", "TGDD", "TGDE", "TGDF")
                               if self._optional(k) is not None]
            self.mode_keys = all(self._optional(f"F{i}Md") is not None for i in range(self.count))
            if not self.mode_keys:
                self.smc.read("FS! ")
            self.sample()
        except BaseException:
            self.close()
            raise

    def _optional(self, key: str) -> float | None:
        try:
            return self.smc.read(key)
        except FanError:
            return None

    def _temperature(self, key: str) -> float | None:
        value = self._optional(key)
        return value if value is not None and 1 <= value <= 115 else None

    def sample(self) -> Sample:
        sensors = {key: self.smc.read(key) for key in self.cpu_keys}
        guards = [v for key in self.guard_keys if (v := self._temperature(key)) is not None]
        mask = 0 if self.mode_keys else int(self.smc.read("FS! "))
        fans = [Fan(i, self.smc.read(f"F{i}Mn"), self.smc.read(f"F{i}Mx"), self.smc.read(f"F{i}Ac"),
                    self.smc.read(f"F{i}Tg"), int(self.smc.read(f"F{i}Md")) if self.mode_keys else int(bool(mask & (1 << i))))
                for i in range(self.count)]
        result = Sample(sensors, self.source, max([*sensors.values(), *guards]), fans)
        result.validate()
        return result

    def preflight(self) -> None:
        if not self.owns_fans and any(f.mode != 0 for f in self.sample().fans):
            raise FanError("其他程序已接管风扇。请先将 Macs Fan Control 恢复自动模式并退出。")

    def apply(self, targets: list[float]) -> None:
        current = self.sample()
        if len(targets) != self.count:
            raise FanError("风扇数量不一致。")
        for fan, target in zip(current.fans, targets):
            if not math.isfinite(target) or not fan.minimum <= target <= fan.maximum:
                raise FanError("目标转速超出硬件范围。")
        self.preflight()
        self.owns_fans = True  # Set before writing, including partial failure.
        # Switch mode first: some firmware ignores target writes in auto mode.
        if self.mode_keys:
            for i in range(self.count):
                if current.fans[i].mode != 1:
                    self.smc.write(f"F{i}Md", 1)
        else:
            mask = int(self.smc.read("FS! "))
            manual = mask | ((1 << self.count) - 1)
            if manual != mask:
                self.smc.write("FS! ", manual)
        for i, target in enumerate(targets):
            self.smc.write(f"F{i}Tg", target)
        # The SMC may report the previous value briefly after a successful write.
        # Bound confirmation well below the client timeout and keep ownership until
        # either every mode/target is confirmed or the caller restores auto mode.
        deadline = time.monotonic() + 1.2
        while True:
            mask = 0 if self.mode_keys else int(self.smc.read("FS! "))
            observed = [(int(self.smc.read(f"F{i}Md")) if self.mode_keys else int(bool(mask & (1 << i))),
                         self.smc.read(f"F{i}Tg")) for i in range(self.count)]
            mismatches = [(i, mode, readback, target)
                          for i, ((mode, readback), target) in enumerate(zip(observed, targets))
                          if mode != 1 or abs(readback - target) > 150]
            if not mismatches:
                break
            if time.monotonic() >= deadline:
                details = "；".join(f"风扇{i + 1}：目标{target:.0f}→回读{readback:.0f}RPM，模式{mode}（{'系统' if mode == 0 else '手动' if mode == 1 else '未知'}）"
                                   for i, mode, readback, target in mismatches)
                raise FanError("SMC 未确认写入，自动调速已停止。" + details)
            time.sleep(0.1)

    def restore(self) -> None:
        if not self.owns_fans:
            return
        errors = []
        if self.mode_keys:
            for i in range(self.count):
                try:
                    self.smc.write(f"F{i}Md", 0)
                except FanError as error:
                    errors.append(str(error))
        else:
            try:
                self.smc.write("FS! ", int(self.smc.read("FS! ")) & ~((1 << self.count) - 1))
            except FanError as error:
                errors.append(str(error))
        if not errors:
            # Restoration must not depend on a failed temperature sensor.
            try:
                if self.mode_keys:
                    automatic = all(self.smc.read(f"F{i}Md") == 0 for i in range(self.count))
                else:
                    automatic = not (int(self.smc.read("FS! ")) & ((1 << self.count) - 1))
                if not automatic:
                    errors.append("SMC 未确认系统自动模式")
            except FanError as error:
                errors.append(str(error))
        if errors:
            raise FanError("恢复系统控制失败，将继续重试：" + "；".join(errors))
        self.owns_fans = False

    def close(self) -> None:
        self.smc.close()


class DemoHardware:
    def __init__(self) -> None:
        self.owns_fans = False
        self.targets: list[float] | None = None
        self.started = time.monotonic()

    def sample(self) -> Sample:
        temperature = 62 + 13 * math.sin((time.monotonic() - self.started) / 15)
        sensors = {f"TC{i}C": temperature + i - 4.5 for i in range(1, 9)}
        fans = [Fan(0, 1836, 5616, 2800, 2800, int(self.owns_fans)),
                Fan(1, 1700, 5200, 2600, 2600, int(self.owns_fans))]
        if self.targets:
            for fan, target in zip(fans, self.targets):
                fan.actual = fan.target = target
        return Sample(sensors, "演示：8 个模拟核心", max(sensors.values()) + 3, fans)

    def preflight(self) -> None:
        pass

    def apply(self, targets: list[float]) -> None:
        self.owns_fans = True
        self.targets = targets

    def restore(self) -> None:
        self.owns_fans = False
        self.targets = None

    def close(self) -> None:
        pass


class Session:
    def __init__(self, hardware: Any, now: float) -> None:
        self.hardware = hardware
        self.controller = Controller()
        self.config = Config()
        self.last_heartbeat = now
        self.last_tick = -math.inf
        self.last_restore = -math.inf
        self.active = False
        self.exit_requested = False
        self.ok = True
        self.message = "系统自动控制"
        self.sample: Sample | None = None
        self.decision: Decision | None = None

    @property
    def can_exit(self) -> bool:
        return self.exit_requested and not self.hardware.owns_fans

    def response(self) -> dict:
        return {"ok": self.ok, "active": self.active, "message": self.message,
                "owns_fans": self.hardware.owns_fans,
                "sample": self.sample.to_dict() if self.sample else None,
                "smoothed": self.decision.smoothed if self.decision else None,
                "emergency": self.decision.emergency if self.decision and self.active else False}

    def _restore(self, now: float) -> None:
        if not self.hardware.owns_fans:
            return
        self.last_restore = now
        try:
            self.hardware.restore()
            if not self.hardware.owns_fans and self.sample:
                for fan in self.sample.fans:
                    fan.mode = 0
        except Exception as error:
            self.ok = False
            self.message = str(error)

    def stop(self, reason: str, now: float, exit_requested: bool = False) -> None:
        self.active = False
        self.exit_requested |= exit_requested
        self.message = reason
        self.controller.reset()
        self.decision = None
        self._restore(now)

    def handle(self, request: dict, now: float) -> dict:
        try:
            if not isinstance(request, dict):
                raise FanError("请求格式无效。")
            command = request.get("command")
            if command == "enable":
                if self.exit_requested:
                    raise FanError("控制服务正在退出，请重新开启。")
                config = Config.from_dict(request.get("config"))
                if self.hardware.owns_fans and not self.active:
                    raise FanError("正在重试恢复系统控制，暂不能重新开启。")
                self.hardware.preflight()
                self.config = config
                self.controller.reset()
                self.last_heartbeat = now
                self.last_tick = -math.inf
                self.active = True
                self.tick(now)
            elif command == "heartbeat":
                self.last_heartbeat = now
            elif command == "status":
                pass  # A status query is intentionally not a heartbeat.
            elif command in ("disable", "shutdown"):
                self.ok = True
                self.stop("系统自动控制", now, command == "shutdown")
            else:
                raise FanError("不支持的控制命令。")
        except Exception as error:
            self.ok = False
            if isinstance(request, dict) and request.get("command") == "enable":
                self.stop(str(error), now)
            else:
                self.message = str(error)
        return self.response()

    def tick(self, now: float) -> None:
        if now - self.last_heartbeat > HEARTBEAT_TIMEOUT and not self.exit_requested:
            self.stop("终端失联，已请求恢复系统控制。", now, True)
        if not self.active and self.hardware.owns_fans and now - self.last_restore >= 1:
            self._restore(now)
        if now - self.last_tick < SAMPLE_INTERVAL:
            return
        self.last_tick = now
        try:
            self.sample = self.hardware.sample()
            self.sample.validate()
            if self.active:
                self.decision = self.controller.update(self.sample, self.config, now)
                self.hardware.apply(self.decision.targets)
                self.sample = self.hardware.sample()
                self.ok = True
                self.message = "热点保护：优先满速" if self.decision.emergency else "正在按 CPU 平均温度调速"
        except Exception as error:
            self.sample = None
            self.ok = False
            self.stop(str(error), now)


class PowerWatcher:
    """Observe sleep in the root helper, independently of terminal health."""
    CALLBACK = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p)

    def __init__(self, on_sleep: Any) -> None:
        self.io = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.port = C.c_void_p()
        self.notifier = C.c_uint32()
        self.allow = _bind(self.io, "IOAllowPowerChange", [C.c_uint32, C.c_ssize_t], C.c_uint32)

        def callback(refcon: Any, service: int, message: int, argument: Any) -> None:
            if message in (0xE0000270, 0xE0000280):
                try:
                    if message == 0xE0000280:
                        on_sleep()
                finally:
                    self.allow(self.root, int(argument or 0))

        self.callback = self.CALLBACK(callback)
        register = _bind(self.io, "IORegisterForSystemPower", [C.c_void_p, C.POINTER(C.c_void_p),
                         self.CALLBACK, C.POINTER(C.c_uint32)], C.c_uint32)
        self.root = register(None, C.byref(self.port), self.callback, C.byref(self.notifier))
        if not self.root or not self.port:
            raise FanError("无法订阅休眠通知，自动调速未开启。")
        source_get = _bind(self.io, "IONotificationPortGetRunLoopSource", [C.c_void_p], C.c_void_p)
        self.source = source_get(self.port)
        self.loop = _bind(self.cf, "CFRunLoopGetCurrent", [], C.c_void_p)()
        self.mode = C.c_void_p.in_dll(self.cf, "kCFRunLoopDefaultMode").value
        _bind(self.cf, "CFRunLoopAddSource", [C.c_void_p, C.c_void_p, C.c_void_p], None)(self.loop, self.source, self.mode)
        self.run = _bind(self.cf, "CFRunLoopRunInMode", [C.c_void_p, C.c_double, C.c_bool], C.c_int32)

    def poll(self) -> None:
        self.run(self.mode, 0.001, False)

    def close(self) -> None:
        _bind(self.cf, "CFRunLoopRemoveSource", [C.c_void_p, C.c_void_p, C.c_void_p], None)(self.loop, self.source, self.mode)
        _bind(self.io, "IODeregisterForSystemPower", [C.POINTER(C.c_uint32)], C.c_uint32)(C.byref(self.notifier))
        _bind(self.io, "IOServiceClose", [C.c_uint32], C.c_uint32)(self.root)
        _bind(self.io, "IONotificationPortDestroy", [C.c_void_p], None)(self.port)


def _send_response(response: dict) -> bool:
    try:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def run_helper() -> int:
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        raise FanError("控制服务必须由终端界面的管理员授权流程启动。")
    # Suspending a terminal must not also suspend the safety watchdog.
    for sig in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
        signal.signal(sig, signal.SIG_IGN)
    try:
        os.setsid()
    except OSError:
        pass
    lock_path = "/var/run/local.FanCurve.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.set_inheritable(lock_fd, False)
    watcher = None
    hardware = None
    session = None
    stopping = False

    def stop_signal(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop_signal)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FanError("另一个 FanCurve 控制服务正在运行。请先在原终端恢复系统控制并退出。") from error
        hardware = Hardware()
        session = Session(hardware, time.monotonic())
        watcher = PowerWatcher(lambda: session.stop("电脑进入休眠，自动模式已停用。", time.monotonic()))
        session.tick(time.monotonic())
        if not _send_response(session.response()):
            session.stop("终端已关闭。", time.monotonic(), True)
        pending = b""
        stdin_open = True
        while not session.can_exit:
            watcher.poll()
            now = time.monotonic()
            if stopping and not session.exit_requested:
                session.stop("收到退出信号。", now, True)
            session.tick(now)
            if not stdin_open:
                time.sleep(0.1)
                continue
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                stdin_open = False
                session.stop("终端已关闭，恢复系统控制。", time.monotonic(), True)
                continue
            pending += chunk
            if len(pending) > 8192:
                stdin_open = False
                session.stop("控制请求过长。", time.monotonic(), True)
                continue
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    request = json.loads(line)
                    response = session.handle(request, time.monotonic())
                except (ValueError, TypeError):
                    response = {"ok": False, "message": "控制请求格式无效。", "active": session.active}
                if not _send_response(response):
                    stdin_open = False
                    session.stop("终端已关闭。", time.monotonic(), True)
                    break
        return 0
    finally:
        # Unexpected exceptions also restore, retrying while ownership remains.
        if session and hardware and hardware.owns_fans:
            session.stop("控制服务退出，恢复系统控制。", time.monotonic(), True)
            while hardware.owns_fans:
                time.sleep(1)
                session._restore(time.monotonic())
        if watcher:
            watcher.close()
        if hardware:
            hardware.close()
        os.close(lock_fd)


class HelperClient:
    def __init__(self) -> None:
        executable = str(Path(sys.executable).resolve())
        script = str(Path(__file__).resolve())
        # Argument vector, not shell interpolation. sudo owns the password prompt.
        self.process = subprocess.Popen(["/usr/bin/sudo", "-n", executable, "-I", "-u", script, "--helper"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.pending = b""
        try:
            response = self.read_response(timeout=6)
            if not response.get("ok"):
                raise FanError(response.get("message", "控制服务初始化失败。"))
        except BaseException:
            if self.process.stdin:
                self.process.stdin.close()
            raise

    def read_response(self, timeout: float = 2) -> dict:
        deadline = time.monotonic() + timeout
        assert self.process.stdout
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FanError("控制服务未及时响应；失联后会尝试恢复系统控制。")
            ready, _, _ = select.select([self.process.stdout.fileno()], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 8192)
            if not chunk:
                detail = ""
                if self.process.poll() is not None and self.process.stderr:
                    detail = self.process.stderr.read(4096).decode("utf-8", errors="replace").strip()
                raise FanError(detail or "控制服务已结束。")
            self.pending += chunk
            if len(self.pending) > 65536:
                raise FanError("控制服务响应过长。")
        line, self.pending = self.pending.split(b"\n", 1)
        response = json.loads(line)
        if not isinstance(response, dict):
            raise FanError("控制服务响应格式无效。")
        return response

    def request(self, command: str, config: Config | None = None) -> dict:
        data: dict[str, Any] = {"command": command}
        if config:
            data["config"] = dataclasses.asdict(config)
        assert self.process.stdin
        try:
            self.process.stdin.write((json.dumps(data, allow_nan=False) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise FanError("控制服务已断开；失联后会尝试恢复系统控制。") from error
        return self.read_response(timeout=4 if command == "enable" else 2)

    def close(self) -> str | None:
        warning = None
        try:
            response = self.request("shutdown")
            if not response.get("ok") or response.get("owns_fans"):
                warning = response.get("message", "恢复系统控制尚未确认。")
        except (FanError, OSError, ValueError) as error:
            warning = str(error)
        finally:
            if self.process.stdin:
                self.process.stdin.close()
        if not warning:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                warning = "控制服务仍在重试恢复系统控制。"
        return warning


def load_config() -> Config:
    try:
        return Config.from_dict(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, FanError, TypeError):
        return Config()


def save_config(config: Config) -> None:
    config.validate()
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(dataclasses.asdict(config), indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def _clip(text: str, width: int) -> str:
    result, used = [], 0
    for char in text:
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def _wrap(text: str, width: int, limit: int = 2) -> list[str]:
    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < limit:
        line = _clip(remaining, width)
        if len(line) < len(remaining):
            separator = max(line.rfind("。"), line.rfind("；"))
            if separator >= max(6, len(line) // 2):
                line = line[:separator + 1]
        lines.append(line)
        remaining = remaining[len(line):]
    if remaining and lines:
        lines[-1] = _clip(lines[-1], max(0, width - 1)) + "…"
    return lines


_LARGE_DIGITS = {
    "0": ("████", "█  █", "█  █", "█  █", "████"),
    "1": ("  █ ", " ██ ", "  █ ", "  █ ", "████"),
    "2": ("████", "   █", "████", "█   ", "████"),
    "3": ("████", "   █", " ███", "   █", "████"),
    "4": ("█  █", "█  █", "████", "   █", "   █"),
    "5": ("████", "█   ", "████", "   █", "████"),
    "6": ("████", "█   ", "████", "█  █", "████"),
    "7": ("████", "   █", "  █ ", " █  ", " █  "),
    "8": ("████", "█  █", "████", "█  █", "████"),
    "9": ("████", "█  █", "████", "   █", "████"),
    ".": ("  ", "  ", "  ", "██", "██"),
    "–": ("    ", "    ", "████", "    ", "    "),
}


def large_temperature(value: float | None) -> tuple[str, ...]:
    display = f"{value:.1f}" if value is not None and math.isfinite(value) else "––.–"
    return tuple(" ".join(_LARGE_DIGITS[char][row] for char in display) for row in range(5))


class TerminalUI:
    def __init__(self, demo: bool) -> None:
        self.demo = demo
        self.config = load_config()
        self.hardware: Any = None
        self.client: HelperClient | None = None
        self.demo_session: Session | None = None
        self.controller = Controller()
        self.sample: Sample | None = None
        self.smoothed: float | None = None
        self.active = False
        self.emergency = False
        self.message = "正在读取硬件…"
        self.error = False
        self.owns_fans = False
        self.enable_error: str | None = None
        self.notice: str | None = None
        self.notice_error = False
        self.edit: Config | None = None
        self.edit_field = 0
        self.help = False
        self.history: deque[float] = deque(maxlen=60)
        self.last_refresh = -math.inf
        self.exit_warning: str | None = None

    def accept(self, response: dict) -> None:
        self.active = bool(response.get("active"))
        self.owns_fans = bool(response.get("owns_fans"))
        self.emergency = bool(response.get("emergency"))
        self.error = not bool(response.get("ok", True))
        self.message = str(response.get("message", ""))
        if self.active:
            self.enable_error = None
        elif self.error:
            self.enable_error = self.message
        self.sample = Sample.from_dict(response["sample"]) if response.get("sample") else None
        self.smoothed = response.get("smoothed")
        if self.smoothed is None and self.sample:
            decision = self.controller.update(self.sample, self.config, time.monotonic())
            self.smoothed = decision.smoothed

    def refresh(self, now: float) -> None:
        self.last_refresh = now
        try:
            if self.client:
                self.accept(self.client.request("heartbeat"))
            elif self.demo_session:
                self.demo_session.handle({"command": "heartbeat"}, now)
                self.demo_session.tick(now)
                self.accept(self.demo_session.response())
            else:
                if self.hardware is None:
                    self.hardware = DemoHardware() if self.demo else Hardware()
                self.sample = self.hardware.sample()
                self.smoothed = self.controller.update(self.sample, self.config, now).smoothed
                self.active = False
                foreign = any(f.mode != 0 for f in self.sample.fans)
                self.message = "其他程序正在控制风扇" if foreign else "系统自动控制 · 当前只监测"
                self.error = False
            if self.smoothed is not None:
                self.history.append(self.smoothed)
        except (FanError, OSError, ValueError) as error:
            was_controlling = self.active or self.owns_fans
            self.message = str(error)
            self.error = True
            if was_controlling:
                self.enable_error = self.message
            self.sample = None
            self.smoothed = None
            self.active = False
            if self.client:
                # Close stdin so the helper restores immediately; never kill it.
                self.exit_warning = self.client.close()
                self.client = None
                if self.exit_warning is None:
                    self.owns_fans = False
            elif self.hardware:
                self.hardware.close()
                self.hardware = None
            self.controller.reset()

    def enable(self, screen: Any) -> None:
        if not self.sample:
            self.message = "没有有效温度数据，不能开启自动调速。"
            self.error = True
            self.enable_error = self.message
            return
        requested = False
        try:
            self.config.validate()
            if self.demo:
                if not self.demo_session:
                    self.demo_session = Session(self.hardware, time.monotonic())
                self.accept(self.demo_session.handle({"command": "enable", "config": dataclasses.asdict(self.config)}, time.monotonic()))
                if self.active:
                    self.notice = None
                    self.notice_error = False
                return
            if not self.client:
                if any(f.mode != 0 for f in self.sample.fans):
                    raise FanError("请先将 Macs Fan Control 恢复自动模式并退出，再开启自动调速。")
                curses.def_prog_mode()
                curses.endwin()
                try:
                    print("\n开启自动调速需要管理员授权。密码由 macOS sudo 验证。\n", flush=True)
                    completed = subprocess.run(["/usr/bin/sudo", "-v"], check=False)
                    if completed.returncode:
                        raise FanError("管理员授权取消或失败。")
                    self.client = HelperClient()
                finally:
                    curses.reset_prog_mode()
                    screen.clear()
                    screen.refresh()
            requested = True
            self.accept(self.client.request("enable", self.config))
            if self.active:
                self.notice = None
                self.notice_error = False
        except (FanError, OSError, ValueError) as error:
            self.message = str(error)
            self.error = True
            if requested and self.client:
                self.exit_warning = self.client.close()
                self.client = None
                self.active = False
                if self.exit_warning is None:
                    self.owns_fans = False
            if self.active:
                self.notice = "无法应用设置：" + self.message
                self.notice_error = True
            else:
                self.enable_error = self.message

    def disable(self) -> None:
        try:
            if self.client:
                self.accept(self.client.request("disable"))
            elif self.demo_session:
                self.accept(self.demo_session.handle({"command": "disable"}, time.monotonic()))
            elif not self.owns_fans:
                self.error = False
            self.controller.reset()
            self.smoothed = self.sample.average if self.sample else None
            if not self.owns_fans and not self.error:
                self.enable_error = None
                self.notice = "已恢复系统控制；已保存的曲线仅供预览。"
                self.notice_error = False
        except (FanError, OSError, ValueError) as error:
            self.message = str(error)
            self.error = True
            if self.client:
                self.exit_warning = self.client.close()
                self.client = None
                self.active = False
                if self.exit_warning is None:
                    self.owns_fans = False
            self.enable_error = self.message

    def status_banner(self) -> tuple[str, str, str, str]:
        if self.owns_fans and not self.active:
            return ("恢复系统控制中 · 曲线未生效", "[s] 请求恢复系统控制", self.enable_error or self.message, "error")
        if self.notice_error and self.active:
            return ("设置未保存 · 当前曲线仍生效", "[c] 修改设置    [s] 恢复系统控制", self.notice or self.message, "error")
        if self.active:
            return ("自动调速已开启 · 曲线正在生效", "[s] 恢复系统控制", self.message, "active")
        if self.enable_error:
            return ("启用失败 · 曲线未生效", "[a] 重试自动调速    [s] 恢复系统控制", self.enable_error, "error")
        if self.notice_error:
            return ("设置未保存 · 曲线未生效", "[c] 修改设置", self.notice or self.message, "error")
        if self.sample is None:
            if self.error:
                return ("读取失败 · 曲线未生效", "请检查硬件读数后重试", self.message, "error")
            return ("正在读取 · 曲线未生效", "[a] 开启自动调速（读取成功后可用）", self.message, "inactive")
        if any(f.mode != 0 for f in self.sample.fans):
            return ("其他程序正在控制风扇 · 曲线未生效", "请先退出其他风扇调速程序", self.message, "error")
        action = "[a] 开启模拟自动调速（无需授权）" if self.demo else "[a] 授权并开启自动调速"
        return ("系统控制 · 曲线未生效", action, self.notice or "当前只监测；下方曲线和转速仅供预览。", "inactive")

    def text(self, screen: Any, row: int, col: int, text: str, style: int = 0) -> None:
        height, width = screen.getmaxyx()
        if 0 <= row < height and 0 <= col < width - 1:
            try:
                screen.addstr(row, col, _clip(text, width - col - 1), style)
            except curses.error:
                pass

    def draw(self, screen: Any) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        accent = curses.color_pair(1) | curses.A_BOLD
        good = curses.color_pair(2)
        warning = curses.color_pair(3)
        dim = curses.A_DIM
        self.text(screen, 0, 2, "FanCurve  /  CPU 温度与风扇", accent)
        for row, line in enumerate(large_temperature(self.smoothed), 1):
            self.text(screen, row, 2, line, accent if self.smoothed is not None else dim)
        self.text(screen, 5, 28, "°C", accent if self.smoothed is not None else dim)
        title, action, detail, tone = self.status_banner()
        status_style = good if tone == "active" else warning if tone == "error" else dim
        self.text(screen, 1, 32, title, status_style | curses.A_BOLD)
        self.text(screen, 2, 32, action, accent)
        for offset, line in enumerate(_wrap("原因：" + detail if tone == "error" else detail,
                                            max(1, width - 34), limit=3)):
            self.text(screen, 3 + offset, 32, line, warning if tone == "error" else dim)
        if height < 24 or width < 78:
            self.text(screen, 7, 2, "请将终端调整至至少 78 列 × 24 行。")
            self.text(screen, height - 2, 2, "[a] 自动调速   [s] 系统控制   [q] 退出", accent)
            screen.refresh()
            return
        if self.help:
            lines = ["快捷键", "a  开启按温度自动调速（演示模式无需授权）", "s  恢复系统风扇控制", "c  修改温度与平滑时间", "设置中：↑↓ 选择，←→ 修改，Enter 保存，Esc 取消", "q  退出并请求恢复系统控制", "? / Esc  返回主界面", "", "平均温度 = 有效 CPU 核心读数的算术平均，再作滚动平均。",
                     "0% 为风扇硬件最低转速；100% 为硬件最高转速。", "CPU/GPU 已知热点 ≥90°C 优先满速，低于85°C退出保护。", "退出、休眠、读数异常、失联12秒会尝试恢复系统控制。", "不要与其他风扇调速软件同时控制同一台 Mac。"]
            for i, line in enumerate(lines):
                self.text(screen, 7 + i, 2, line, accent if i == 0 else 0)
        else:
            self.text(screen, 6, 2, f"CPU 平均温度 · {self.config.window:g} 秒平滑", dim)
            if self.sample:
                self.text(screen, 6, 34, f"当前均温 {self.sample.average:.1f}°C · 热点 {self.sample.hottest:.1f}°C")
                self.text(screen, 8, 2, self.sample.source, dim)
            self.text(screen, 7, 2, "演示模式 · 模拟数据" if self.demo else "Intel Mac · 无需编译 · 默认只监测", dim)
            for col, label in ((2, "风扇"), (12, "实际RPM"), (24, "当前目标"), (36, "曲线预计"),
                               (48, "硬件范围"), (65, "控制")):
                self.text(screen, 9, col, label, accent)
            if self.sample:
                for i, fan in enumerate(self.sample.fans):
                    row = 10 + i
                    mode = "FanCurve" if self.active else ("系统" if fan.mode == 0 else "其他程序")
                    expected = fan.minimum + (fan.maximum - fan.minimum) * self.config.fraction(self.smoothed) if self.smoothed is not None else None
                    for col, value in ((2, f"风扇 {fan.id + 1}"), (12, f"{fan.actual:.0f}"),
                                       (24, f"{fan.target:.0f}"),
                                       (36, f"{expected:.0f}" if expected is not None else "—"),
                                       (48, f"{fan.minimum:.0f}–{fan.maximum:.0f}"), (65, mode)):
                        self.text(screen, row, col, value, 0 if col != 36 or self.active else dim)
            else:
                self.text(screen, 10, 2, "暂无硬件数据", dim)
            extra_fans = max(0, len(self.sample.fans) - 2) if self.sample else 0
            graph_top = 14 + extra_fans
            graph_width = min(width - 16, 70)
            if graph_top + 9 < height:
                self.text(screen, graph_top - 1, 2, "温度曲线已生效" if self.active else "温度曲线预览 · 当前未生效", accent if self.active else dim)
                for y in range(5):
                    self.text(screen, graph_top + y, 2, f"{100-y*25:3d}%", dim)
                    self.text(screen, graph_top + y, 8, "·" * graph_width, dim)
                for x in range(graph_width):
                    t = 30 + x / (graph_width - 1) * 70
                    y = 4 - round(self.config.fraction(t) * 4)
                    self.text(screen, graph_top + y, 8 + x, "━", accent if self.active else dim)
                if self.smoothed is not None:
                    x = round(max(0, min(1, (self.smoothed - 30) / 70)) * (graph_width - 1))
                    y = 4 - round(self.config.fraction(self.smoothed) * 4)
                    self.text(screen, graph_top + y, 8 + x, "●" if self.active else "○", good | curses.A_BOLD if self.active else dim)
                self.text(screen, graph_top + 5, 8, "30°C" + " " * max(0, graph_width - 10) + "100°C", dim)
                self.text(screen, graph_top + 6, 2, f"开始加速 {self.config.start:g}°C  →  满速 {self.config.full:g}°C     [c] 修改曲线")
                if self.history:
                    symbols = "▁▂▃▄▅▆▇█"
                    chart = "".join(symbols[round(max(0, min(1, (v - 30) / 70)) * 7)] for v in self.history)
                    self.text(screen, graph_top + 7, 2, "最近温度  " + chart, accent if self.active else dim)
            else:
                self.text(screen, 19, 2, "风扇较多，请增大终端高度查看温度曲线。", dim)
            if self.edit:
                self.text(screen, graph_top - 1, 2, "设置  /  ↑↓ 选择 · ←→ 调整 · Enter 保存 · Esc 取消", accent)
                for i, (label, value, unit) in enumerate([( "开始加速", self.edit.start, "°C"),
                                                          ("达到满速", self.edit.full, "°C"),
                                                          ("平滑时间", self.edit.window, "秒")]):
                    self.text(screen, graph_top + i, 8, ("> " if i == self.edit_field else "  ") + f"{label}：{value:g} {unit}" + " " * (graph_width - 18), accent if i == self.edit_field else 0)
        footer = max(22, 22 + (max(0, len(self.sample.fans) - 2) if self.sample and height >= 24 + len(self.sample.fans) - 2 else 0))
        footer = min(footer, height - 2)
        self.text(screen, footer, 2, "[a] 自动调速    [s] 系统控制    [c] 设置    [?] 帮助    [q] 退出", accent)
        self.text(screen, footer + 1, 2, "0%=硬件最低转速；退出与休眠时恢复系统控制。", dim)
        screen.refresh()

    def edit_key(self, key: int) -> None:
        assert self.edit
        if key == 27:
            self.edit = None
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            self.edit_field = (self.edit_field + (1 if key == curses.KEY_DOWN else -1)) % 3
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            fields = ["start", "full", "window"]
            field = fields[self.edit_field]
            step = 2 if field == "window" else 1
            value = getattr(self.edit, field) + (step if key == curses.KEY_RIGHT else -step)
            self.edit = dataclasses.replace(self.edit, **{field: value})
        elif key in (10, 13, curses.KEY_ENTER):
            saved = False
            try:
                self.edit.validate()
                save_config(self.edit)
                saved = True
                self.config = self.edit
                self.edit = None
                self.controller.reset()
                self.notice = None
                self.notice_error = False
                if self.active:
                    if self.client:
                        self.accept(self.client.request("enable", self.config))
                    elif self.demo_session:
                        self.accept(self.demo_session.handle({"command": "enable", "config": dataclasses.asdict(self.config)}, time.monotonic()))
                else:
                    self.message = "曲线已保存；当前仍由系统控制风扇。"
                    self.error = False
                    self.notice = "曲线已保存，但尚未生效。按 [a] 授权并开启自动调速。"
                    self.notice_error = False
            except (FanError, OSError) as error:
                self.message = str(error)
                self.error = True
                if saved:
                    if self.client:
                        self.exit_warning = self.client.close()
                        self.client = None
                        self.active = False
                        if self.exit_warning is None:
                            self.owns_fans = False
                    self.enable_error = self.message
                else:
                    self.notice = "设置错误：" + self.message
                    self.notice_error = True

    def run(self, screen: Any) -> None:
        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(100)
        curses.start_color()
        curses.use_default_colors()
        for i, color in enumerate([curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW], 1):
            curses.init_pair(i, color, -1)
        while True:
            now = time.monotonic()
            if now - self.last_refresh >= SAMPLE_INTERVAL:
                self.refresh(now)
            self.draw(screen)
            key = screen.getch()
            if key == ord("q"):
                break
            if self.edit:
                self.edit_key(key)
            elif self.help:
                if key in (ord("?"), 27):
                    self.help = False
                elif key == ord("s"):
                    self.disable()
            elif key == ord("a"):
                self.enable(screen)
            elif key == ord("s"):
                self.disable()
            elif key == ord("c"):
                self.edit = dataclasses.replace(self.config)
            elif key == ord("?"):
                self.help = True

    def close(self) -> None:
        if self.client:
            self.exit_warning = self.client.close()
            self.client = None
        if self.demo_session:
            self.demo_session.stop("退出演示", time.monotonic(), True)
        if self.hardware:
            self.hardware.close()
        if self.exit_warning:
            print("\n" + self.exit_warning, file=sys.stderr)
        else:
            print("\nFanCurve 已退出。" + ("演示未操作真实风扇。" if self.demo else ""))


def simulate() -> None:
    controller = Controller()
    config = Config()
    print("模拟数据 / 未访问硬件\nCPU     平滑均温   风扇1 RPM  风扇2 RPM  保护")
    for i, temperature in enumerate([40, 50, 60, 70, 80, 93, 87, 83, 65, 45]):
        sample = Sample({"DEMO": temperature}, "模拟", temperature,
                        [Fan(0, 1800, 5500, 1800, 1800), Fan(1, 1600, 5000, 1600, 1600)])
        d = controller.update(sample, config, i * 2)
        print(f"{temperature:3.0f}°C   {d.smoothed:5.1f}°C    {d.targets[0]:6.0f}     {d.targets[1]:6.0f}     {'满速' if d.emergency else '—'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Intel Mac CPU 均温风扇控制，带终端界面，无需编译。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--probe", action="store_true", help="只读探测硬件，输出 JSON")
    modes.add_argument("--demo", action="store_true", help="使用模拟数据体验终端界面")
    modes.add_argument("--simulate", action="store_true", help="输出模拟调速过程")
    modes.add_argument("--helper", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.simulate:
            simulate()
        elif args.probe:
            hardware = Hardware()
            try:
                print(json.dumps(hardware.sample().to_dict(), ensure_ascii=False, indent=2, allow_nan=False))
            finally:
                hardware.close()
        elif args.helper:
            return run_helper()
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise FanError("交互界面需要终端，请双击 启动.command 或在终端中运行；可用 --probe / --simulate。")
            if os.geteuid() == 0:
                raise FanError("请使用普通用户启动界面，按 a 时再申请管理员授权。")
            try:
                locale.setlocale(locale.LC_ALL, "")
            except locale.Error:
                # Some launch environments inherit a locale macOS doesn't ship.
                locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
            ui = TerminalUI(args.demo)
            try:
                curses.wrapper(ui.run)
            finally:
                ui.close()
        return 0
    except KeyboardInterrupt:
        return 130
    except (FanError, OSError, ValueError, curses.error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
