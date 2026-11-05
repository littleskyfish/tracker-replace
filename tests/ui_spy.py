# -*- coding: utf-8 -*-
"""无头 UI 冒烟：真建窗口，检查每个按钮/勾选框都在窗口内（不越界被裁），
并走一遍自检步骤，确认新排版不炸。"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracker_ui  # noqa: E402

SPY = {"bad": None, "n": 0}
orig_tk = tk.Tk


class SpyTk(orig_tk):
    def __init__(self, *a, **k):
        orig_tk.__init__(self, *a, **k)
        self.bind("<Map>", self._onmap, add="+")
        self._spied = False

    def _onmap(self, e):
        if e.widget is not self or self._spied:
            return
        self._spied = True
        self.after(500, self._measure)

    def _measure(self):
        try:
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            rx, ry = self.winfo_rootx(), self.winfo_rooty()
            bad, n = [], 0
            want = []
            stack = list(self.winfo_children())
            while stack:
                c = stack.pop()
                stack.extend(c.winfo_children())
                cls = c.winfo_class()
                if cls in ("TButton", "TCheckbutton", "Button", "Checkbutton"):
                    n += 1
                    try:
                        ax = c.winfo_rootx() - rx
                        ay = c.winfo_rooty() - ry
                        cw, ch = c.winfo_width(), c.winfo_height()
                        if ax < 0 or ay < 0 or ax + cw > w + 1 or ay + ch > h + 1:
                            bad.append((c.cget("text"), ax, ay, cw, ch))
                        else:
                            want.append(c.cget("text"))
                    except Exception:
                        pass
            SPY["bad"], SPY["n"] = bad, n
            print("UI-SPY 窗口 %dx%d，按钮/勾选 %d 个，越界 %s" % (w, h, n, bad if bad else "0 个"))
        except Exception as e:
            SPY["bad"] = [("spy-except", str(e)[:80])]
            print("UI-SPY 异常", e)


tk.Tk = SpyTk
try:
    tracker_ui.run_tk(tracker_ui.load_conf(), selftest=["clearlog", "status"])
except SystemExit:
    pass
bad, n = SPY["bad"], SPY["n"]
print("UI-SPY 结论：控件 %d 个；越界 %s" % (n, bad if bad else "无"))
sys.exit(1 if (bad or n < 12) else 0)
