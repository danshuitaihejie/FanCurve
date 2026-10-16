# FanCurve

FanCurve 是一款面向 Intel Mac 的命令行风扇控制工具，提供终端界面（TUI）。只支持 Intel 架构的 macOS。普通启动会在有效采样后尝试开启调速并请求管理员授权；运行 `--install` 可一次授权安装受保护 helper 和登录项，此后每次登录会自动开启调速，先隐藏终端窗口，在菜单栏持续显示温度，无需再次输入密码。

## 启动

推荐使用 `uv` 启动，它会使用 Python 标准库运行程序，不需要安装第三方 Python 包：

```sh
git clone https://github.com/danshuitaihejie/FanCurve.git
cd FanCurve
uv run --no-project fan_curve.py
```

也可以双击 `启动.command`。启动器会优先使用 PATH 中的 `uv`，其次尝试用户目录下的 `$HOME/.local/bin/uv`；找不到 `uv` 时会使用 `python3`。

如需只监测，使用 `uv run --no-project fan_curve.py --monitor`；仍可按 `a` 手动启用调速。`--demo` 使用模拟数据，并默认自动启用模拟调速，不会操作真实风扇。
手动运行时窗口默认可见；如果也想先隐藏它，可加 `--start-hidden`。

## 登录时启动

需要 macOS 自带的 `/usr/bin/python3` 版本不低于 3.9，并且 `/private/etc/sudoers` 已启用 `/private/etc/sudoers.d`（通常是 `#includedir /private/etc/sudoers.d`）。如果首次运行触发 `xcode-select` 安装提示，请先完成 macOS 命令行工具安装，再重新运行；Python 无法启动与版本过旧会分别提示。将 `fan_curve.py` 复制到 Intel Mac 上希望长期保存的位置，然后在 Terminal 中运行：

```sh
uv run --no-project fan_curve.py --install
```

安装期间需要输入一次管理员密码。`--install` 会把受保护的控制脚本和固定启动器安装到 `/Library/PrivilegedHelperTools`，并在 `/private/etc/sudoers.d/fancurve` 写入仅允许当前用户无密码、无参数启动该固定 helper 的规则；如果这些目录尚不存在，安装器会核验其父目录权限后创建，并用 `visudo` 校验规则。随后会在 `~/Library/Application Support/FanCurve/启动.command` 创建可执行启动器，并在 `~/Library/LaunchAgents/com.fancurve.login.plist` 创建当前用户登录项。登录项会在后台打开 Terminal，读取有效温度、启动菜单栏图标和自动调速后，只最小化 FanCurve 所在的终端窗口。安装本身不会启动调速，也不会保存管理员密码。

如果移动了 `fan_curve.py` 或更新了风扇控制服务逻辑，请在新位置重新运行 `uv run --no-project fan_curve.py --install`，以更新受保护副本和登录启动器引用的路径。仅更新终端或菜单栏界面时，替换原脚本并重启程序即可。运行 `uv run --no-project fan_curve.py --uninstall` 可移除受保护 helper、sudoers 规则、登录项和启动器；卸载需要一次管理员授权，并保留原脚本及配置。

安装完成后，FanCurve 的终端进程会在最小化的窗口中继续运行；点击菜单栏温度可恢复同一个窗口，再次最小化仍会继续调速。按 `q` 退出或关闭该终端窗口会结束程序，菜单栏图标随之消失，并请求恢复系统风扇控制。控制服务由 root 权限的 helper 执行，登录后无需再次输入密码。纯终端界面不能在用户登录前运行。电脑休眠时控制会停用；受保护模式会在唤醒后自动尝试重新开启一次，若失败不会循环重试，可按 `a` 手动再试。

## 界面操作

- `a`：请求开启或重试自动调速。默认启动时程序会在首次有效采样后自动执行一次相同的授权和开启流程。程序会先暂停终端界面，通过 `sudo -v` 请求授权，然后以非交互方式启动仅在本次会话运行的 helper；只有成功开启后才会接管风扇，不会安装常驻服务。开启失败的原因会保留在顶部状态区域。
- `s`：恢复系统风扇控制。
- `c`：打开设置。使用上下方向键选择字段、左右方向键调整，按 Enter 保存，按 Esc 取消。保存只更新曲线参数，不会自动开启控制；需按 `a` 并成功开启自动模式后曲线才会生效。自动模式运行时保存的新配置会立即应用。
- `?`：查看帮助。
- `q`：退出。

界面左上角以 5 行块状大数字显示按当前设置平滑后的 CPU 均温（默认 6 秒），右侧显示自动模式状态、操作提示和错误详情。运行期间，macOS 菜单栏也会显示温度计图标和同一个平滑均温（如 `69.5°C`）；尚未读到有效温度时显示 `--.-°C`。点击图标会显示 FanCurve 对应的 Terminal 窗口，不会误打开其他终端窗口。菜单栏显示随程序启动和退出，不需要额外安装 Python 包。若菜单栏显示无法启动，终端底部会提示，窗口不会自动隐藏。

风扇表中的“当前目标”表示硬件当前报告的目标转速；“曲线预计”表示按平滑后的 CPU 均温和曲线计算出的目标，仅供参考。热点保护触发时，“当前目标”可能高于“曲线预计”。未开启自动模式时，曲线图会以灰色空心点标出预计位置，表示该曲线尚未控制风扇。

默认曲线在 50°C 到 80°C 之间，将 CPU 平均温度线性映射到风扇硬件报告的最低和最高转速。温度采用 6 秒平滑，控制采样和界面刷新间隔均为 2 秒。终端窗口至少需要 78 列 × 24 行。CPU 或 GPU 已知热点达到 90°C 时优先满速，所有已知热点低于 85°C 后退出保护。SMC 写入后会在短时间内重读模式与目标转速；若仍无法确认，程序停止自动调速、请求恢复系统控制，并显示各风扇请求与回读的数值。

Macs Fan Control 已安装时，请先在其中恢复自动模式并退出，避免控制器争抢风扇。按 `q` 退出时程序会请求恢复系统风扇控制；helper 失去 12 秒心跳后会尝试恢复系统风扇控制并退出；恢复失败时会继续重试，但不能绝对保证恢复成功。进程被 `SIGKILL` 强制终止时无法保证执行恢复操作。

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

测试覆盖由 `tests/test_fan_curve.py` 提供，包括仅在临时目录中构建和移除受保护安装文件的测试。硬件写入需管理员授权；文档中的模拟和只读诊断不代表已完成真实设备风扇写入验证。

SMC 接口结构参考 [SMCKit](https://github.com/beltex/SMCKit)，通过 Python `ctypes` 独立实现。核心温度不可用时会采用 CPU 封装传感器，并在界面中注明。配置保存在程序旁的 `.fancurve.json`；普通模式每次启动都会重新请求自动调速授权，已安装受保护 helper 时登录后可无人值守开启。
