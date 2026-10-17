#!/bin/sh
cd "$(dirname "$0")" || exit 1
FANCURVE_DIR=$PWD
export UV_CACHE_DIR="$FANCURVE_DIR/.work/uv-cache"
FANCURVE_LANG=${FANCURVE_LANG:-zh}

if command -v uv >/dev/null 2>&1; then
    exec uv run --no-project fan_curve.py --lang "$FANCURVE_LANG"
elif [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" run --no-project fan_curve.py --lang "$FANCURVE_LANG"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 fan_curve.py --lang "$FANCURVE_LANG"
else
    if [ "$FANCURVE_LANG" = en ]; then
        printf '%s\n' 'Cannot start FanCurve: neither uv nor python3 was found.' >&2
    else
        printf '%s\n' '找不到 uv 或 python3，无法启动 FanCurve。' >&2
    fi
    exit 127
fi
