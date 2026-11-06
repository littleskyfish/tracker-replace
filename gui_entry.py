# -*- coding: utf-8 -*-
"""exe 的入口：双击就打网页窗口。

命令行用法还是走 tracker_replace.py（或 -cli 那个 exe）。这里只做一件事：
不带参数时补一个 `ui`，否则双击 exe 会因为缺参数直接退出（黑框一闪，看不见报错）。
"""
import sys

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("ui")
    from tracker_replace import main
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
