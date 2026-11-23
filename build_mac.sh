#!/bin/sh
# macOS 打单文件可执行：产出 dist/tracker-replace
# （PyInstaller 不能跨平台交叉编译，这个脚本必须在 Mac 上跑；Intel/Apple 芯片均可）
# 只需要 python3（3.8+，brew install python 或官网安装包，自带 tkinter）。
set -e
cd "$(dirname "$0")"
python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --onefile --clean --noupx --name tracker-replace \
    --hidden-import tracker_ui \
    --hidden-import tkinter \
    tracker_replace.py
echo
echo "完成：dist/tracker-replace"
echo "  窗口界面   ./dist/tracker-replace gui"
echo "  网页窗口   ./dist/tracker-replace ui        （没有图形库时用这个）"
echo "  命令行     ./dist/tracker-replace doctor --old old-tracker.example"
echo
echo "提示：从网上下载/拷贝过去首次运行若被 Gatekeeper 拦："
echo "  xattr -dr com.apple.quarantine dist/tracker-replace"
