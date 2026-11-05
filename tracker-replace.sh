#!/bin/sh
# Linux/macOS 直接跑源码（不用打包）
exec python3 "$(dirname "$0")/tracker_replace.py" "$@"
