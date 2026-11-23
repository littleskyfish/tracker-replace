#!/bin/sh
# macOS 双击直接跑源码、开原生窗口界面（不用打包，对应 Windows 的 .bat）
# 第一次双击若被 Gatekeeper 拦：右键点它 → 打开。若提示没有 python3：
# 终端跑一句 xcode-select --install 装完再来双击。
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=/usr/bin/python3
fi
"$PY" tracker_replace.py gui
