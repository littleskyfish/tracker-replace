#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tracker_replace 的窗口界面 —— 两种，随你挑：

    python3 tracker_replace.py gui      原生窗口（tkinter）。Linux 单文件版、装了 Python 的 Windows 可用
    python3 tracker_replace.py ui       本地网页窗口（自动开浏览器，只监听 127.0.0.1）
                                —— 不需要 tkinter，绿色免安装包用的就是这个

界面只是换了张脸，干活仍然是 tracker_replace.py 里那套流程和安全规则：
  先体检 → 再演练 → 才执行；数据文件永不删除；
  校验没过 / 状态不对 → 停住保持暂停，绝不自动下载；通过才自动做种。
"""
import argparse
import contextlib
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime

import tracker_replace

CONF = os.path.join(tracker_replace.HERE, "guiconfig.json")
FORM_DEFAULTS = {"old": "", "new_url": "", "tdir": "", "only": "", "limit": 0,
                 "seed_after": True, "budget": 1800, "poll": 5, "sleep": 1,
                 "maxfail": 5, "nocap": False, "layout": "", "hash": ""}


# --------------------------------------------------------------- 配置
def load_conf():
    try:
        c = json.load(open(CONF, encoding="utf-8"))
    except Exception:
        c = {}
    for k, v in FORM_DEFAULTS.items():
        c.setdefault(k, v)
    c.setdefault("instances", [])
    return c


def save_conf(c):
    try:
        json.dump(c, open(CONF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        try:
            os.chmod(CONF, 0o600)                    # 里面有密码，别让别人读
        except OSError:
            pass
        return True
    except Exception:
        return False


def write_client_config(instances):
    """把界面上勾了的实例写成 tracker_replace 认识的 config.json（引擎只吃这个）"""
    p = os.path.join(tracker_replace.HERE, "clients.json")
    rows = []
    for i in instances:
        if not (i.get("host") and i.get("port")):
            continue
        r = {"name": i.get("name") or "%s:%s" % (i.get("host"), i.get("port")),
             "host": i.get("host"), "port": int(i.get("port")),
             "scheme": (i.get("scheme") or "http").lower(),
             "insecure": bool(i.get("insecure")),
             "user": i.get("user") or "", "password": i.get("password") or ""}
        if i.get("kind"):
            r["kind"] = i["kind"]
        rows.append(r)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"instances": rows}, fh, ensure_ascii=False, indent=1)
    try:
        os.chmod(p, 0o600)                              # 里面有密码，别让别人读
    except OSError:
        pass
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p, rows


# --------------------------------------------------------------- 跑任务
class Engine(object):
    """一次只能跑一个任务；日志进队列，界面自己取。"""

    def __init__(self, sink=None):
        self.sink = sink or (lambda s: None)
        self.busy = False
        self.thread = None
        self.lines = []
        self.lock = threading.Lock()

    def emit(self, text):
        for ln in str(text).splitlines() or [""]:
            self.lines.append(ln)
        self.lines = self.lines[-4000:]
        try:
            self.sink(text)
        except Exception:
            pass

    def form_args(self, form, instances, cmd):
        cfg, rows = write_client_config(instances)
        if not rows:
            raise ValueError("先在实例列表里加至少一台（地址/端口/账号）")
        seed_after = form.get("seed_after", True)
        return argparse.Namespace(
            cmd="apply" if cmd in ("dry", "run") else cmd,
            config=cfg, host=None, port=None, user="", password="", kind=None,
            old=(form.get("old") or "").strip(),
            new_url=(form.get("new_url") or "").strip(),
            torrents_dir=(form.get("tdir") or "").strip() or None,
            csv=None, only=(form.get("only") or "").strip(),
            limit=int(form.get("limit") or 0),
            yes=(cmd == "run"), dry_run=False,
            pause=(not seed_after),
            layout=(form.get("layout") or "").strip(),
            check_budget=int(form.get("budget") or 1800),
            poll=float(form.get("poll") or 5), sleep=float(form.get("sleep") or 1),
            max_fail=int(form.get("maxfail") or 5),
            hash=(form.get("hash") or "").strip(), to_old=bool(form.get("to_old")),
            rb_force=bool(form.get("rb_force")), rb_keep=bool(form.get("rb_keep")),
            no_cap=bool(form.get("nocap")), wait_limit=int(form.get("wait_limit") or 30))

    def start(self, cmd, form, instances):
        if self.busy:
            self.emit("★ 上一个任务还没跑完，等它结束再说")
            return False
        try:
            ns = self.form_args(form, instances, cmd)
        except Exception as e:
            self.emit("★ 参数不对：%s" % e)
            return False
        if cmd == "run" and not form.get("confirmed"):
            self.emit("★ 这是【真执行】。确认无误后把“我已确认”勾上再点。")
            return False
        if cmd == "run":
            self.emit("★ 真执行：会删旧条目（数据文件不动），日志请留着别关")
        self.busy = True
        self.thread = threading.Thread(target=self._work, args=(cmd, ns), daemon=True)
        self.thread.start()
        return True

    def _work(self, cmd, ns):
        sink = lambda s: self.emit(s)                      # noqa: E731
        tracker_replace.set_logger(sink)
        buf = _Writer(sink)
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(buf):
                if cmd == "doctor":
                    tracker_replace.cmd_doctor(ns)
                elif cmd in ("dry", "run"):
                    tracker_replace.cmd_apply(ns)
                elif cmd == "status":
                    tracker_replace.cmd_status(ns)
                elif cmd == "rollback":
                    tracker_replace.cmd_rollback(ns)
                elif cmd == "repair":
                    tracker_replace.cmd_repair(ns)
        except SystemExit as e:
            code = e.code
            if code not in (0, None):
                sink("（结束，退出码 %s —— 有异常项，明细见上/结果表）" % code)
        except Exception as e:
            sink("★ 出错了：%s" % str(e)[:300])
        finally:
            tracker_replace.set_logger(None)
            self.busy = False
            sink("―――― 这一步用了 %.0f 秒 ――――" % (time.time() - t0))


class _Writer(object):
    """把 print() 的东西也送进日志框"""

    def __init__(self, sink):
        self.sink = sink
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.sink(line)
        return len(s)

    def flush(self):
        if self.buf.strip():
            self.sink(self.buf)
        self.buf = ""


# --------------------------------------------------------------- 原生窗口
def run_tk(conf, selftest=None):
    """selftest=['doctor','dry']：无头自检 —— 真建窗口、真按按钮、把日志抓下来"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    eng = Engine()
    q = queue.Queue()
    eng.sink = lambda s: q.put(str(s))

    root = tk.Tk()
    root.title("tracker替换 %s —— 批量换 tracker（qB / Transmission）" % tracker_replace.VERSION)
    root.geometry("1020x700")

    insts = list(conf.get("instances", []))
    vars_ = {k: tk.StringVar(value=str(conf.get(k, "")))
             for k in ("old", "new_url", "tdir", "only", "layout", "hash")}
    v_limit = tk.StringVar(value=str(conf.get("limit", 0)))
    v_budget = tk.StringVar(value=str(conf.get("budget", 1800)))
    v_poll = tk.StringVar(value=str(conf.get("poll", 5)))
    v_sleep = tk.StringVar(value=str(conf.get("sleep", 1)))
    v_maxfail = tk.StringVar(value=str(conf.get("maxfail", 5)))
    v_seed = tk.BooleanVar(value=bool(conf.get("seed_after", True)))
    v_nocap = tk.BooleanVar(value=bool(conf.get("nocap", False)))
    v_rbforce = tk.BooleanVar(value=False)
    v_rbkeep = tk.BooleanVar(value=False)
    v_confirm = tk.BooleanVar(value=False)

    def collect_form():
        f = {"old": vars_["old"].get().strip(), "new_url": vars_["new_url"].get().strip(),
             "tdir": vars_["tdir"].get().strip(), "only": vars_["only"].get().strip(),
             "layout": vars_["layout"].get().strip(), "hash": vars_["hash"].get().strip(),
             "seed_after": v_seed.get(), "nocap": v_nocap.get(), "confirmed": v_confirm.get(),
             "rb_force": v_rbforce.get(), "rb_keep": v_rbkeep.get()}
        for k, v in (("limit", v_limit), ("budget", v_budget), ("poll", v_poll),
                     ("sleep", v_sleep), ("maxfail", v_maxfail)):
            try:
                f[k] = type(FORM_DEFAULTS[k])(float(v.get() or 0))
            except ValueError:
                f[k] = FORM_DEFAULTS[k]
        return f

    def save_form():
        c = load_conf()
        c["instances"] = insts
        c.update({k: v for k, v in collect_form().items() if k != "confirmed"})
        save_conf(c)
        push("界面设置已存到 %s" % CONF)

    # ---- 左：实例 ----
    left = ttk.LabelFrame(root, text="客户端实例（先加进来，可多台一起干）")
    left.pack(fill="x", padx=8, pady=4)
    cols = ("kind", "addr", "user")
    tree = ttk.Treeview(left, columns=cols, show="headings", height=5)
    for c, w, t in (("kind", 60, "类型"), ("addr", 250, "协议://地址:端口"), ("user", 100, "账号")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w")
    tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)

    def refresh():
        tree.delete(*tree.get_children())
        for i in insts:
            tree.insert("", "end", values=(i.get("kind") or "自动",
                                           "%s://%s:%s" % (i.get("scheme") or "http",
                                                           i.get("host"), i.get("port")),
                                           i.get("user") or "-"))
    refresh()

    lb = ttk.Frame(left)
    lb.pack(side="right", fill="y", padx=4)
    sel = lambda: tree.focus()                                     # noqa: E731

    def edit(title, src=None, on_ok=None):
        d = tk.Toplevel(root)
        d.title(title)
        d.geometry("400x280")
        d.transient(root)
        g = {"proto": tk.StringVar(value=(src or {}).get("scheme") or "http"),
             "kind": tk.StringVar(value=(src or {}).get("kind", "")),
             "name": tk.StringVar(value=(src or {}).get("name", "")),
             "host": tk.StringVar(value=(src or {}).get("host", "")),
             "port": tk.StringVar(value=str((src or {}).get("port", ""))),
             "user": tk.StringVar(value=(src or {}).get("user", "")),
             "pw": tk.StringVar(value=(src or {}).get("password", ""))}
        ins = tk.BooleanVar(value=bool((src or {}).get("insecure")))
        rowi = 0
        ttk.Label(d, text="协议").grid(row=rowi, column=0, sticky="w", padx=8, pady=3)
        ttk.Combobox(d, textvariable=g["proto"], values=("http", "https"),
                     state="readonly", width=7).grid(row=rowi, column=1, sticky="w",
                                                     padx=8, pady=3)
        ttk.Checkbutton(d, text="跳过证书校验(自签)", variable=ins).grid(
            row=rowi, column=1, sticky="e", padx=8)
        rowi += 1
        for key, label, show in (("kind", "类型(留空=自动识别)", ""),
                                 ("name", "备注名", ""),
                                 ("host", "地址(域名或IP)", ""), ("port", "端口", ""),
                                 ("user", "用户名", ""), ("pw", "密码", "*")):
            ttk.Label(d, text=label).grid(row=rowi, column=0, sticky="w", padx=8, pady=3)
            ttk.Entry(d, textvariable=g[key], show=show, width=28).grid(
                row=rowi, column=1, sticky="ew", padx=8, pady=3)
            rowi += 1
        ttk.Label(d, text="密码存在本机 guiconfig.json（0600）", foreground="#666").grid(
            row=rowi, column=0, columnspan=2, sticky="w", padx=8)

        def ok(_e=None):
            if not g["host"].get().strip() or not g["port"].get().strip():
                messagebox.showwarning("差东西", "地址和端口要填")
                return
            obj = {"kind": g["kind"].get().strip(), "name": g["name"].get().strip(),
                   "scheme": g["proto"].get().strip() or "http",
                   "insecure": bool(ins.get()),
                   "host": g["host"].get().strip(), "port": int(g["port"].get().strip()),
                   "user": g["user"].get().strip(), "password": g["pw"].get()}
            d.destroy()
            on_ok(obj)

        ttk.Button(d, text="好", command=ok).grid(row=rowi + 1, column=1, sticky="e", padx=8, pady=8)
        d.bind("<Return>", ok)

    def add_i():
        edit("添加实例", None, lambda o: (insts.append(o), refresh(), save_form()))

    def edit_i():
        k = sel()
        if not k:
            return
        i = tree.index(k)
        edit("修改实例", insts[i], lambda o: (insts.__setitem__(i, o), refresh(), save_form()))

    def del_i():
        k = sel()
        if k:
            insts.pop(tree.index(k))
            refresh()
            save_form()

    for t, c in (("添加", add_i), ("修改", edit_i), ("删除", del_i)):
        ttk.Button(lb, text=t, command=c).pack(fill="x", pady=2)

    # ---- 中：任务参数 ----
    mid = ttk.LabelFrame(root, text="任务")
    mid.pack(fill="x", padx=8, pady=4)

    def row(parent, items, r):
        c = 0
        for label, var, w in items:
            ttk.Label(parent, text=label).grid(row=r, column=c, sticky="e", padx=(6, 2), pady=3)
            c += 1
            ttk.Entry(parent, textvariable=var, width=w).grid(row=r, column=c, sticky="w", pady=3)
            c += 1
        return c

    row(mid, [("旧 tracker 含", vars_["old"], 26), ("期望新 tracker", vars_["new_url"], 26)], 0)
    row(mid, [("新种子目录", vars_["tdir"], 34)], 1)
    ttk.Button(mid, text="浏览…", command=lambda: (
        vars_["tdir"].set(filedialog.askdirectory(title="放站点新下载的 .torrent 的目录")
                          or vars_["tdir"].get()), None)).grid(row=1, column=4, padx=4)
    row(mid, [("只处理含", vars_["only"], 18), ("本次上限", v_limit, 6),
              ("校验超时秒", v_budget, 7)], 2)
    row(mid, [("轮询秒", v_poll, 5), ("每个间隔秒", v_sleep, 5),
              ("连错几个收工", v_maxfail, 5), ("回滚 hash", vars_["hash"], 20)], 3)

    def pick_rollback():
        win = tk.Toplevel(root)
        win.title("选回滚种子（双击一行，hash 自动填进输入框）")
        top = ttk.Frame(win)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="旧工具目录：").pack(side="left")
        e = ttk.Entry(top, width=52)
        e.pack(side="left", fill="x", expand=True)

        def browse():
            p = filedialog.askopenfilename(
                title="选旧版 exe（或任选旧工具文件夹里的一个文件）",
                initialdir=tracker_replace.HERE) or \
                filedialog.askdirectory(title="或者选旧版工具所在的文件夹",
                                        initialdir=tracker_replace.HERE)
            if p:
                e.delete(0, "end")
                e.insert(0, p)

        def refresh(added=None):
            lb.delete(0, "end")
            for i, x in enumerate(rows):
                lb.insert("end", "%d %s %s %-42s %s %s" % (i + 1, "✗" if x["failed"] else "✓",
                          x["hash"], (x["name"] or "")[:42], x["ts"], x["inst"]))
            if not rows:
                lb.insert("end", "（没有备份——没跑过 apply 就没有可回滚的；旧版工具"
                                 "的备份可以把上面路径填进来或浏览挑一个）")

        def adddir():
            p = e.get().strip()
            if not p:
                messagebox.showwarning("差路径", "先把旧版工具文件夹（或旧 exe）的路径填进来，"
                                       "或点浏览挑一个")
                return
            try:
                got = tracker_replace.add_state_dir(p)
            except ValueError as err:
                messagebox.showwarning("没找到备份", str(err))
                return
            rows[:] = tracker_replace.scan_backups()
            refresh()
            status.configure(text="已把旧备份目录加进来：%s" % got)
            e.delete(0, "end")

        ttk.Button(top, text="浏览…", command=browse).pack(side="left", padx=4)
        ttk.Button(top, text="加进来重新扫", command=adddir).pack(side="left")
        rows = tracker_replace.scan_backups()
        lb = tk.Listbox(win, width=110, height=14, font=("TkFixedFont", 10))

        def use(_ev=None):
            sel = lb.curselection()
            if sel and sel[0] < len(rows):
                vars_["hash"].set(rows[sel[0]]["hash"])
                status.configure(text="已把 %s 填进回滚 hash" % rows[sel[0]]["hash"][:12])
                win.destroy()
        lb.bind("<Double-Button-1>", use)
        refresh()
        ttk.Button(win, text="把选中的填进 hash 框", command=use).pack(fill="x")
        lb.pack(fill="both", expand=True)

    opt = ttk.Frame(mid)
    opt.grid(row=4, column=0, columnspan=6, sticky="w", pady=4)
    ttk.Checkbutton(opt, text="校验通过就自动开始做种", variable=v_seed).pack(side="left", padx=6)
    ttk.Checkbutton(opt, text="TR 校验期不加 1KB/s 限速", variable=v_nocap).pack(side="left", padx=6)
    ttk.Checkbutton(opt, text="我已确认，可以动真格", variable=v_confirm).pack(side="left", padx=6)
    ttk.Button(opt, text="选回滚种子", command=pick_rollback).pack(side="left", padx=6)
    ttk.Checkbutton(opt, text="回滚遇不一致→强制覆盖", variable=v_rbforce).pack(side="left", padx=2)
    ttk.Checkbutton(opt, text="→保持不动", variable=v_rbkeep).pack(side="left", padx=2)

    def import_old():
        d = filedialog.askdirectory(title="选旧版工具所在的文件夹（里面要有 state/backup）",
                                    initialdir=tracker_replace.HERE)
        if not d:
            return
        try:
            rep = tracker_replace.import_old(d)
        except ValueError as err:
            messagebox.showwarning("导不了", str(err))
            return
        try:                       # 实例可能被导入扩充：回读配置、刷新左边列表
            fresh = json.load(open(tracker_replace.GUI, encoding="utf-8"))
            insts[:] = [i for i in fresh.get("instances", []) if isinstance(i, dict)]
            conf["instances"] = list(insts)
            if rep.get("tdir") and not vars_["tdir"].get().strip():
                vars_["tdir"].set(rep["tdir"])   # 旧版填过的新种子目录：自家是空的才补
            refresh()
        except Exception:
            pass
        messagebox.showinfo("导入完成",
                            "备份 +%d（跳过已有 %d）\n客户端实例 +%d（已列进左边）\n台账条目 +%d、备份目录 +%d%s\n\n"
                            "旧版文件夹原样没动，以后可以不管它了。"
                            % (rep["files"], rep["same"], rep["inst"], rep["ledger"],
                               rep.get("sdirs", 0),
                               ("\n新种子目录已填：%s" % rep["tdir"]) if rep.get("tdir") else ""))
        status.configure(text="已从旧版导入：备份 %d、实例 %d、台账 %d"
                         % (rep["files"], rep["inst"], rep["ledger"]))

    ttk.Button(opt, text="导入旧版备份/配置", command=import_old).pack(side="left", padx=6)
    ttk.Button(opt, text="存下这套设置", command=save_form).pack(side="right", padx=6)

    # ---- 按钮 ----
    btns = ttk.Frame(root)
    btns.pack(fill="x", padx=8)
    status = ttk.Label(root, text="空闲")
    status.pack(anchor="w", padx=10)

    # ---- 日志 ----
    logf = ttk.LabelFrame(root, text="日志")
    logf.pack(fill="both", expand=True, padx=8, pady=6)
    txt = tk.Text(logf, wrap="word", font=("TkFixedFont", 10))
    sb = ttk.Scrollbar(logf, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.tag_configure("warn", foreground="#b00")
    txt.tag_configure("ok", foreground="#070")

    def clear_log():
        txt.delete("1.0", "end")
        txt.insert("end", "（日志区已清空；后台要是有任务在跑，新日志会继续出现）\n")

    ttk.Button(btns, text="清空日志", command=clear_log).pack(side="right", padx=4)

    def push(s):
        q.put(s)

    eng.sink = push

    def do(cmd):
        if eng.start(cmd, collect_form(), insts):
            status.config(text="正在跑：%s …（别关窗口）" % {"doctor": "体检", "dry": "演练",
                                                            "run": "真执行", "status": "看台账",
                                                            "rollback": "回滚",
                                                            "repair": "一键修复"}[cmd])

    for t, c, col in (("① 体检", "doctor", "#0a6"), ("② 演练", "dry", "#06a"),
                      ("③ 真执行", "run", "#a00"), ("台账", "status", "#555"),
                      ("回滚这个 hash", "rollback", "#a60"),
                      ("一键修复丢失", "repair", "#070")):
        ttk.Button(btns, text=t, command=lambda x=c: do(x)).pack(side="left", padx=4, pady=4)

    def drain():
        try:
            while True:
                s = q.get_nowait()
                for ln in str(s).splitlines():
                    tag = ()
                    if ("✗" in ln or "★" in ln or "⚠" in ln or "停止" in ln):
                        tag = ("warn",)
                    elif "✓" in ln:
                        tag = ("ok",)
                    txt.insert("end", ln + "\n", tag)
                txt.see("end")
        except queue.Empty:
            pass
        status.config(text=("正在跑…" if eng.busy else "空闲"))
        root.after(180, drain)

    if not insts:
        push("还没有实例：点【添加】把你机器上的 qB / TR 加进来（WebUI 地址、端口、账号、密码）。")
        push("顺序：① 体检 → ② 演练（看看要动哪些）→ 勾上“我已确认” → ③ 真执行。")

    if selftest:                                    # 无头自检用：自己把按钮按一遍
        steps = list(selftest)
        box = {"log": "", "fail": 1, "done": False}
        root.after(180, drain)                      # 没有这行，日志进不了文本框，自检会假绿

        def auto():
            if eng.busy:
                root.after(200, auto)
                return
            if not steps:
                box["log"] = txt.get("1.0", "end")
                bad = any(k in box["log"] for k in ("★ 出错了", "Traceback"))
                box["fail"] = 0 if (box["log"].strip() and not bad
                                    and not box.get("hard")) else 1
                box["done"] = True
                root.destroy()
                return
            st = steps.pop(0)
            if st == "clearlog":                    # 自检用：按一下「清空日志」
                clear_log()
            elif st == "importold":                 # 自检：一键导入旧版备份/实例/台账
                try:
                    rep = tracker_replace.import_old(conf.get("far", ""))
                except ValueError as err:
                    box["hard"] = 1
                    txt.insert("end", "自检 importold ✗ %s\n" % err)
                else:
                    want = conf.get("want", "zz")
                    hit = any(r["hash"].startswith(want)
                              for r in tracker_replace.scan_backups())
                    own = rep["files"] >= 1 and hit
                    if own:
                        rep2 = tracker_replace.import_old(conf.get("far", ""))
                        try:       # 导完必须立刻刷进左边列表（和历史 bug 对着干）
                            fresh = json.load(open(tracker_replace.GUI, encoding="utf-8"))
                            insts[:] = [i for i in fresh.get("instances", []) if isinstance(i, dict)]
                            conf["instances"] = list(insts)
                            refresh()
                            if len(tree.get_children()) < len(insts) or \
                                    (rep2["inst"] + rep["inst"] and len(insts) == 0):
                                box["hard"] = 1
                                txt.insert("end", "自检 importold ✗ 导完列表没刷新\n")
                        except Exception as e:
                            box["hard"] = 1
                            txt.insert("end", "自检 importold ✗ 刷新列表炸了：%s\n" % e)
                        txt.insert("end", "自检 importold ✓ 备份 %d、实例 %d、台账 %d；"
                                   "再导一次不重复（+%d）\n"
                                   % (rep["files"], rep["inst"], rep["ledger"], rep2["files"]))
                        if rep2["files"] != 0:
                            box["hard"] = 1
                    else:
                        box["hard"] = 1
                        txt.insert("end", "自检 importold ✗ 导完列表里找不到导入的东西\n")
            elif st == "pickdlg":                   # 自检：选旧工具目录→备份进列表→开窗不炸
                try:
                    got = tracker_replace.add_state_dir(conf.get("far", ""))
                except ValueError as err:
                    box["hard"] = 1
                    txt.insert("end", "自检 pickdlg ✗ 加目录失败：%s\n" % err)
                else:
                    rows = tracker_replace.scan_backups()
                    if any(r["hash"].startswith(conf.get("want", "zz")) for r in rows):
                        txt.insert("end", "自检 pickdlg ✓ 旧目录 %s 的备份进了列表（共 %d 份）\n"
                                   % (got, len(rows)))
                    else:
                        box["hard"] = 1
                        txt.insert("end", "自检 pickdlg ✗ 加了目录列表里还是没有它\n")
                    pick_rollback()
                    for w in root.winfo_children():
                        if isinstance(w, tk.Toplevel):
                            w.after(50, w.destroy)
            else:
                do(st)
            root.after(200, auto)

        v_confirm.set(True)
        root.after(300, auto)
        root.after(45000, lambda: (box.update({"log": box["log"] + "\n(自检超时)", "done": True}),
                                   root.destroy()))
        root.mainloop()
        run_tk.result = box
        return box["fail"]

    root.after(180, drain)
    root.mainloop()


# --------------------------------------------------------------- 网页窗口
PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tracker替换 · 批量换 tracker</title>
<style>
 body{font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222}
 header{background:#1f2937;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px}
 header b{font-size:16px} header span{color:#9ca3af;font-size:12px}
 main{display:flex;gap:12px;padding:12px;flex-wrap:wrap}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px;box-shadow:0 1px 2px #0001}
 .l{flex:1 1 380px;min-width:360px} .r{flex:2 1 560px;min-width:420px}
 h3{margin:0 0 8px;font-size:14px;color:#374151}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{border-bottom:1px solid #eee;padding:4px 6px;text-align:left}
 tr.sel{background:#eef2ff}
 input,select{padding:4px 6px;border:1px solid #d1d5db;border-radius:4px;font-size:13px}
 label{display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:13px}
 .btn{padding:6px 12px;border:0;border-radius:5px;color:#fff;cursor:pointer;font-size:13px;margin:2px}
 .b1{background:#059669}.b2{background:#2563eb}.b3{background:#dc2626}.b4{background:#6b7280}.b5{background:#d97706}
 .log{background:#111827;color:#d1d5db;font:12px/1.45 ui-monospace,Consolas,monospace;
      height:420px;overflow:auto;padding:8px;border-radius:6px;white-space:pre-wrap}
 .log .w{color:#fca5a5}.log .g{color:#86efac}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0;align-items:center}
 .row span{font-size:13px;color:#4b5563;min-width:88px}
 .hint{color:#6b7280;font-size:12px}
</style></head><body>
<header><b>tracker替换</b><span>批量换 tracker · qBittorrent / Transmission · 数据文件永不删除</span>
<span id="busy" style="margin-left:auto"></span></header>
<main>
 <div class="card l">
  <h3>客户端实例 <span class="hint" id="ver"></span></h3>
  <table id="tb"><thead><tr><th>类型</th><th>协议://地址:端口</th><th>账号</th><th></th></tr></thead><tbody></tbody></table>
  <div class="row" style="margin-top:8px">
   <select id="proto"><option value="http">http</option><option value="https">https</option></select>
   <select id="kind"><option value="">自动识别</option><option value="qb">qBittorrent</option>
   <option value="tr">Transmission</option></select>
   <input id="host" placeholder="地址(域名或IP)" size="14"><input id="port" placeholder="端口" size="6">
   <input id="user" placeholder="用户名" size="10"><input id="pw" placeholder="密码" size="12" type="password">
   <label class="hint"><input type="checkbox" id="insec">跳过证书校验</label>
   <button class="btn b4" onclick="addInst()">添加</button>
  </div>
  <div class="hint">选中某行可删除。配置存在本机 guiconfig.json（权限 0600）。</div>
 </div>
 <div class="card r">
  <h3>任务</h3>
  <div class="row"><span>旧 tracker 含</span><input id="old" size="26" placeholder="比如 old-tracker.example">
   <span>期望新 tracker</span><input id="new_url" size="26"></div>
  <div class="row"><span>新种子目录</span><input id="tdir" size="46" placeholder="站点新下载的 .torrent 放一起的目录">
   <span class="hint">服务器在你那台机器上就填它看得见的目录</span></div>
  <div class="row"><span>只处理含</span><input id="only" size="16">
   <span>本次上限</span><input id="limit" size="5" value="0">
   <span>校验超时秒</span><input id="budget" size="7" value="1800"></div>
  <div class="row"><span>轮询秒</span><input id="poll" size="5" value="5">
   <span>每个间隔秒</span><input id="sleep" size="5" value="1">
   <span>连错几个收工</span><input id="maxfail" size="5" value="5">
   <span>回滚 hash</span><input id="hash" size="18">
   <button type="button" onclick="loadRb()">选回滚种子</button>
   <select id="rb" onchange="if(this.value)document.getElementById('hash').value=this.value"
    style="max-width:380px"><option value="">（从有备份的中挑一个）</option></select>
   <label><input type="checkbox" id="rbforce">遇不一致→强制覆盖</label>
   <label><input type="checkbox" id="rbkeep">→保持不动</label></div>
  <div class="row">
   <input id="sdir" size="30" placeholder="旧版工具文件夹（或旧 exe）的路径">
   <button type="button" onclick="addSD()">指过来扫描</button>
   <button type="button" onclick="impOld()" style="background:#7c3aed">一键导入旧版</button>
   <span class="hint">「扫描」= 每次去看一眼旧目录；「导入」= 备份/实例/台账搬进新版，旧目录以后不用管</span></div>
  <div class="row">
   <label><input type="checkbox" id="seed" checked>校验通过就自动开始做种</label>
   <label><input type="checkbox" id="nocap">TR 校验期不加 1KB/s 限速</label>
   <label><input type="checkbox" id="confirm">我已确认，可以动真格</label></div>
  <div>
   <button class="btn b1" onclick="go('doctor')">① 体检</button>
   <button class="btn b2" onclick="go('dry')">② 演练</button>
   <button class="btn b3" onclick="go('run')">③ 真执行</button>
   <button class="btn b4" onclick="go('status')">台账</button>
   <button class="btn b5" onclick="go('rollback')">回滚这个 hash</button>
   <button class="btn b5" onclick="go('repair')">一键修复丢失种子</button>
  </div>
 </div>
</main>
<main><div class="card" style="flex:1 1 100%">
 <h3>日志 <span class="hint" id="stat">空闲</span>
  <button class="btn b4" style="float:right" onclick="clearLog()">清空日志</button></h3>
 <div class="log" id="log">还没开工。顺序：加实例 → ① 体检 → ② 演练 → 勾“我已确认” → ③ 真执行。</div>
</div></main>
<script>
let S={instances:[],sel:-1};
async function api(p,body){const r=await fetch(p,{method:body?'POST':'GET',
  headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):null});return r.json();}
function render(){const b=document.querySelector('#tb tbody');b.innerHTML='';
 S.instances.forEach((x,i)=>{const tr=document.createElement('tr');if(i==S.sel)tr.className='sel';
  tr.innerHTML=`<td>${x.kind||'自动'}</td><td>${x.scheme||'http'}://${x.host}:${x.port}</td><td>${x.user||'-'}</td>
   <td><button class="btn b4" onclick="del(${i})">删</button></td>`;
  tr.onclick=()=>{S.sel=i;render();};b.appendChild(tr);});}
function addInst(){const h=host.value.trim(),p=parseInt(port.value||0);
 if(!h||!p){alert('地址和端口要填');return;}
 if(!/^https?:\/\//.test(h) && /:\/\//.test(h)){alert('地址只填域名或IP，协议用左边下拉框');return;}
 S.instances.push({scheme:proto.value,kind:kind.value,host:h.replace(/^\w+:\/\//,'').replace(/\/+$/,''),
   port:p,insecure:document.getElementById('insec').checked,
   user:user.value.trim(),password:pw.value});
 api('/api/save',{instances:S.instances});render();}
function del(i){S.instances.splice(i,1);api('/api/save',{instances:S.instances});render();}
function form(){return{old:old.value,new_url:new_url.value,tdir:tdir.value,only:only.value,
 limit:limit.value,budget:budget.value,poll:poll.value,sleep:sleep.value,maxfail:maxfail.value,
 hash:hash.value,seed:seed.checked,nocap:nocap.checked,confirm:confirm.checked,
 rb_force:rbforce.checked,rb_keep:rbkeep.checked};}
async function go(c){const r=await api('/api/run',{cmd:c,form:form(),instances:S.instances});
 if(r.err)log0('★ '+r.err);}
let idx=0;
function clearLog(){const e=document.getElementById('log');e.innerHTML='';
 log0('（日志区已清空；后台要是有任务在跑，新日志会继续出现）');}
function log0(t){const e=document.getElementById('log');const d=document.createElement('div');
 d.textContent=t;if(/[✗★⚠]|停止/.test(t))d.className='w';if(/✓/.test(t))d.className='g';
 e.appendChild(d);e.scrollTop=e.scrollHeight;}
async function tick(){try{const s=await api('/api/state?after='+idx);
  idx=s.index;s.lines.forEach(l=>{if(l!=null)log0(l)});
  document.getElementById('stat').textContent=s.busy?'正在跑…（别关页面）':'空闲';
  document.getElementById('busy').textContent=s.busy?'任务进行中':'';
  if(s.version){document.title='tracker替换 '+s.version+' · 批量换 tracker';
   const v=document.getElementById('ver');if(v)v.textContent='v'+s.version;}
  if(s.instances!==undefined&&S.instances.length==0){S.instances=s.instances;render();}
 }catch(e){}}
setInterval(tick,700);
async function loadRb(){const s=document.getElementById('rb');let rows=[];
 try{rows=await api('/api/rblist');}catch(e){}
 s.innerHTML='';const n0=document.createElement('option');
 n0.value='';n0.textContent=rows.length?'点一行把 hash 填进输入框':'（没有备份：没跑过 apply 就没有可回滚的）';
 s.appendChild(n0);
 rows.forEach(x=>{const o=document.createElement('option');o.value=x.hash;
  o.textContent=(x.failed?'✗':'✓')+' '+x.hash.slice(0,10)+' '+((x.name||'').slice(0,24))+' ['+x.inst+']';
  s.appendChild(o)});}
async function addSD(){const d=document.getElementById('sdir').value.trim();
 if(!d){alert('先把旧版工具文件夹（或旧 exe）的路径填进来');return;}
 const r=await api('/api/statedirs',{dir:d});
 if(r.err)alert(r.err);
 else{document.getElementById('sdir').value='';loadRb();}}
async function impOld(){const d=document.getElementById('sdir').value.trim();
 if(!d){alert('先把旧版工具文件夹（或旧 exe）的路径填进来');return;}
 if(!confirm('把旧版的备份、客户端实例、台账复制进新版？\n旧版文件夹原样不动，导完就可以不管它了。'))return;
 const r=await api('/api/importold',{dir:d});
 if(r.err)alert(r.err);
 else{document.getElementById('sdir').value='';
  if(r.instances){S.instances=r.instances;render();}
  if(r.tdir&&!document.getElementById('tdir').value.trim())document.getElementById('tdir').value=r.tdir;
  loadRb();
  alert('导入完成：备份 +'+r.files+'（跳过已有 '+r.same+'）、实例 +'+r.inst+'（已列进上面列表）、台账 +'+r.ledger+(r.sdirs?'、备份目录 +'+r.sdirs:'')+(r.tdir?'\n新种子目录已填：'+r.tdir:''));}}
(async()=>{const s=await api('/api/state?after=0');S.instances=s.instances||[];render();
  ['old','new_url','tdir','only','limit','budget','poll','sleep','maxfail','hash'].forEach(k=>{
   if(s.form&&s.form[k]!==undefined)document.getElementById(k).value=s.form[k];});
  seed.checked=!!s.form.seed_after;nocap.checked=!!s.form.nocap;})();
</script></body></html>"""


def run_web(conf, port, open_browser=True):
    import http.server
    import socketserver

    state = {"engine": None, "conf": conf}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _html(self, raw):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return {}

        def do_GET(self):
            eng = state["engine"]
            if self.path.startswith("/api/rblist"):
                return self._json(tracker_replace.scan_backups())
            if self.path.startswith("/api/state"):
                after = 0
                try:
                    after = int(self.path.split("after=")[1].split("&")[0])
                except Exception:
                    pass
                lines = eng.lines[after:]
                c = state["conf"]
                return self._json({"index": after + len(lines), "lines": lines,
                                   "busy": eng.busy, "version": tracker_replace.VERSION,
                                   "instances": c.get("instances", []),
                                   "form": {k: c.get(k) for k in FORM_DEFAULTS}})
            if self.path in ("/", "/index.html"):
                return self._html(PAGE.encode("utf-8"))
            return self._json({"err": "nope"}, 404)

        def do_POST(self):
            eng = state["engine"]
            b = self._body()
            if self.path == "/api/statedirs":
                try:
                    p = tracker_replace.add_state_dir(b.get("dir", ""))
                except ValueError as e:
                    return self._json({"err": str(e)})
                try:            # 同步内存快照，免得下次存配置把新目录冲掉
                    state["conf"]["state_dirs"] = json.load(
                        open(tracker_replace.GUI, encoding="utf-8")).get("state_dirs", [])
                except Exception:
                    pass
                return self._json({"dir": p})
            if self.path == "/api/importold":
                try:
                    rep = tracker_replace.import_old(b.get("dir", ""))
                except ValueError as e:
                    return self._json({"err": str(e)})
                try:            # 实例可能被导入扩充，同步内存快照
                    fresh = json.load(open(tracker_replace.GUI, encoding="utf-8"))
                    state["conf"]["instances"] = fresh.get("instances", [])
                    state["conf"]["state_dirs"] = fresh.get("state_dirs", [])
                    if rep.get("tdir"):
                        state["conf"].setdefault("form", {})["tdir"] = rep["tdir"]
                except Exception:
                    pass
                return self._json({"files": rep["files"], "same": rep["same"],
                                   "inst": rep["inst"], "ledger": rep["ledger"],
                                   "tdir": rep.get("tdir", ""),
                                   "instances": state["conf"].get("instances", [])})
            if self.path == "/api/save":
                c = state["conf"]
                c["instances"] = b.get("instances", [])
                save_conf(c)
                return self._json({"ok": True})
            if self.path == "/api/run":
                f = b.get("form") or {}
                form = {"old": f.get("old", ""), "new_url": f.get("new_url", ""),
                        "tdir": f.get("tdir", ""), "only": f.get("only", ""),
                        "hash": f.get("hash", ""), "layout": "",
                        "limit": f.get("limit", 0), "budget": f.get("budget", 1800),
                        "poll": f.get("poll", 5), "sleep": f.get("sleep", 1),
                        "maxfail": f.get("maxfail", 5), "nocap": f.get("nocap"),
                        "seed_after": f.get("seed", True),
                        "confirmed": f.get("confirm")}
                insts = b.get("instances") or state["conf"].get("instances", [])
                state["conf"]["instances"] = insts
                for k in ("old", "new_url", "tdir", "only", "hash", "limit", "budget",
                          "poll", "sleep", "maxfail"):
                    state["conf"][k] = f.get(k, "")
                state["conf"]["seed_after"] = form["seed_after"]
                state["conf"]["nocap"] = bool(form["nocap"])
                save_conf(state["conf"])
                ok = eng.start(b.get("cmd", "dry"), form, insts)
                return self._json({"ok": ok})
            return self._json({"err": "nope"}, 404)

    class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = Srv(("127.0.0.1", port), H)
    eng = Engine()
    state["engine"] = eng
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("=" * 58)
    print("  窗口已就绪，浏览器打开：  %s" % url)
    print("  （只监听本机，别人连不进来；这窗口关掉就停）")
    print("=" * 58, flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n收工")


# --------------------------------------------------------------- 入口
def run(a):
    conf = load_conf()
    st = getattr(a, "selftest", "") or ""
    if a.cmd == "gui":
        steps = ["doctor", "dry"]
        if st:                                      # 无头自检：实例参数从命令行 JSON 进来
            try:
                payload = json.loads(st)
            except ValueError:
                print("--selftest 要接一段 JSON")
                return 2
            conf["instances"] = payload.get("instances", [])
            for k in ("old", "new_url", "tdir", "only"):
                if k in payload:
                    conf[k] = payload[k]
            conf["seed_after"] = payload.get("seed_after", True)
            for k in ("far", "want"):               # pickdlg 自检用
                if k in payload:
                    conf[k] = payload[k]
            want = payload.get("steps")
            if want:                                  # 自检可以指定要按哪几个按钮
                allowed = ("doctor", "dry", "run", "status", "rollback", "repair",
                           "clearlog", "pickdlg", "importold")
                steps = [x for x in want if x in allowed] or steps
            sys.argv = [x for x in sys.argv if x not in ("--selftest", st)]
        try:
            import tkinter                            # noqa: F401
        except ImportError:
            print("这台机器没有 tkinter（图形库），改用网页窗口：\n"
                  "    python3 tracker_replace.py ui\n它会自己打开浏览器，效果一样。")
            return 1
        try:
            rc = run_tk(conf, selftest=steps if st else None)
        except Exception as e:
            msg = str(e).lower()
            if "no display" in msg or "couldn't connect" in msg or "display name" in msg:
                print("这台机器没有图形界面（没 DISPLAY），改用网页窗口：\n"
                      "    python3 tracker_replace.py ui")
                return 1
            raise
        if st:
            box = getattr(run_tk, "result", {}) or {}
            print("---- 窗口里的日志 ----")
            print((box.get("log") or "").strip())
            print("---- 自检结论：%s ----" % ("通过" if not box.get("fail") else "有问题"))
        return rc or 0
    return run_web(conf, getattr(a, "ui_port", 0) or 0,
                   open_browser=not getattr(a, "no_browser", False)) or 0
