# FanCurve

FanCurve 是一款面向 Intel Mac 的命令行风扇控制工具，提供终端界面（TUI）。只支持 Intel 架构的 macOS。程序默认只监测硬件，不会自动写入风扇转速。

## 启动

推荐使用 `uv` 启动，它会使用 Python 标准库运行程序，不需要安装第三方 Python 包：

```sh
git clone https://github.com/danshuitaihejie/FanCurve.git
cd FanCurve
uv run --no-project fan_curve.py
```

也可以双击 `启动.command`。启动器会优先使用 PATH 中的 `uv`，其次尝试用户目录下的 `$HOME/.local/bin/uv`；找不到 `uv` 时会使用 `python3`。

## 界面操作

- `a`：开启自动调速。程序会先暂停终端界面，通过 `sudo -v` 请求管理员授权，然后以非交互方式启动仅在本次会话运行的 helper；不会安装常驻服务。
- `s`：恢复系统风扇控制。
- `c`：打开设置。使用上下方向键选择字段、左右方向键调整，按 Enter 保存，按 Esc 取消。自动模式运行时保存的配置会立即应用。
- `?`：查看帮助。
- `q`：退出。

默认曲线在 50°C 到 80°C 之间，将 CPU 平均温度线性映射到风扇硬件报告的最低和最高转速。温度采用 6 秒平滑，控制采样和界面刷新间隔均为 2 秒。终端窗口至少需要 78 列 × 24 行；30 行以上会显示温度历史。CPU 或 GPU 已知热点达到 90°C 时优先满速，所有已知热点低于 85°C 后退出保护。

Macs Fan Control 已安装时，请先在其中恢复自动模式并退出，避免控制器争抢风扇。电脑休眠后自动模式会停用，唤醒后需要手动重新开启。helper 失去 12 秒心跳后会尝试恢复系统风扇控制并退出；恢复失败时会继续重试，但不能绝对保证恢复成功。进程被 `SIGKILL` 强制终止时无法保证执行恢复操作。

## 诊断与模拟

只读探测硬件并输出 JSON：

```sh
uv run --no-project fan_curve.py --probe
```

在终端中运行模拟 TUI：

```sh
uv run --no-project fan_curve.py --demo
```

输出默认曲线表：

```sh
uv run --no-project fan_curve.py --simulate
```

`--helper` 是程序内部使用的 root 标准输入/输出 JSON 服务入口，用户无需手动运行。

## 测试

```sh
UV_CACHE_DIR=.work/uv-cache uv run --no-project -m unittest discover -s tests -v
```

测试覆盖由 `tests/test_fan_curve.py` 提供。硬件写入需管理员授权；文档中的模拟和只读诊断不代表已完成真实设备风扇写入验证。

SMC 接口结构参考 [SMCKit](https://github.com/beltex/SMCKit)，通过 Python `ctypes` 独立实现。核心温度不可用时会采用 CPU 封装传感器，并在界面中注明。配置保存在程序旁的 `.fancurve.json`；自动模式不会跨程序重启或电脑休眠自动恢复。
