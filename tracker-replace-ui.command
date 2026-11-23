#!/bin/sh
# macOS 双击直接跑源码、开网页窗口界面（浏览器标签页当窗口，对应 Windows 的 ui.bat）
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=/usr/bin/python3
fi
"$PY" tracker_replace.py ui
