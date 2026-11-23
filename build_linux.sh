#!/bin/sh
# Linux 打单文件可执行：产出 dist/tracker-replace
# 只需要 python3（3.7+）；工具本身只用标准库。
# 窗口界面要 tkinter：Debian/Ubuntu apt install python3-tk，CentOS/Anolis yum install python3-tkinter
set -e
cd "$(dirname "$0")"
python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --onefile --clean --noupx --name tracker-replace --hidden-import tracker_ui tracker_replace.py
echo
echo "完成：dist/tracker-replace"
echo "  窗口界面   ./dist/tracker-replace gui"
echo "  网页窗口   ./dist/tracker-replace ui        （机器上没有图形库时用这个）"
echo "  命令行     ./dist/tracker-replace doctor --old old-tracker.example"
