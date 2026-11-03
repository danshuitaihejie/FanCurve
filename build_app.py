#!/usr/bin/env python3
"""Build a relocatable macOS launcher bundle using Apple's bundled applet runtime."""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


BUNDLE_ID = "com.fancurve.app"
VERSION = "1.1.0"


def build_app(output: Path, mode: str | None = None, sign: str = "-", archive: bool = True) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("Building FanCurve.app requires macOS.")
    if output.suffix != ".app" or not output.name.isascii():
        raise ValueError("Use an ASCII application filename ending in .app.")
    if mode not in (None, "demo", "monitor"):
        raise ValueError("Unsupported application mode.")
    output = output.resolve()
    if output.exists():
        info = plistlib.loads((output / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            raise ValueError(f"Refusing to replace an unrelated application: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    mode_argument = f" --{mode}" if mode else ""
    source = '''on run
    launchFanCurve()
end run

on reopen
    launchFanCurve()
end reopen

on launchFanCurve()
    set scriptPath to POSIX path of (path to resource "fan_curve.py")
    try
        do shell script "/usr/bin/python3 -I " & quoted form of scriptPath & " --launch-app --lang autoMODE"
    on error errorMessage number errorNumber
        display alert "FanCurve" message errorMessage as critical
    end try
end launchFanCurve
'''.replace("MODE", mode_argument)

    # Build and sign in a staging directory so a failed build preserves the previous app.
    with tempfile.TemporaryDirectory(prefix="fancurve-build-", dir=output.parent) as directory:
        staging = Path(directory)
        source_path = staging / "launch.applescript"
        source_path.write_text(source, encoding="utf-8")
        bundle = staging / output.name
        subprocess.run(["/usr/bin/osacompile", "-o", str(bundle), str(source_path)], check=True)
        resources = bundle / "Contents/Resources"
        project = Path(__file__).resolve().parent
        for filename in ("fan_curve.py", "LICENSE", "README.md", "README.zh-CN.md"):
            shutil.copy2(project / filename, resources / filename)
        info_path = bundle / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        for key in list(info):
            if key.startswith("NS") and key.endswith("UsageDescription"):
                del info[key]
        info.update({"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "FanCurve",
                     "CFBundleDisplayName": "FanCurve", "CFBundleShortVersionString": VERSION,
                     "CFBundleVersion": VERSION, "CFBundleDevelopmentRegion": "en",
                     "CFBundleLocalizations": ["en", "zh-Hans"], "LSUIElement": True,
                     "LSMinimumSystemVersion": "11.0",
                     "NSAppleEventsUsageDescription":
                         "FanCurve uses its own Terminal profile to display temperatures and fan controls."})
        info_path.write_bytes(plistlib.dumps(info))
        for language, description in {
            "en": "FanCurve opens its dedicated Terminal window for temperatures and fan controls.",
            "zh-Hans": "FanCurve 需要打开专用终端窗口，以显示温度与风扇控制界面。",
        }.items():
            localized = resources / f"{language}.lproj"
            localized.mkdir(exist_ok=True)
            (localized / "InfoPlist.strings").write_text(
                f'"CFBundleDisplayName" = "FanCurve";\n"NSAppleEventsUsageDescription" = "{description}";\n',
                encoding="utf-8")
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", sign,
                        "--timestamp=none", str(bundle)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(bundle)], check=True)
        if any(not path.name.isascii() for path in bundle.rglob("*")):
            raise ValueError("Application contains a non-ASCII filename.")
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(bundle), output)
    if archive:
        zip_path = output.with_name(f"{output.stem}-macOS.zip")
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(output), str(zip_path)], check=True)
        print(zip_path)
    print(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/FanCurve.app"))
    parser.add_argument("--mode", choices=("demo", "monitor"),
                        help="Build an alternate launcher for hardware-free demo or read-only testing.")
    parser.add_argument("--sign", default="-", help="Signing identity; defaults to a local ad-hoc signature.")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()
    build_app(args.output, args.mode, args.sign, not args.no_zip)


if __name__ == "__main__":
    main()
