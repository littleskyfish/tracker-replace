#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tracker_replace 全维度自检 —— 本地仿真 qBittorrent WebUI / Transmission RPC

不碰任何生产客户端，把成功路径和每一种失败路径都真跑一遍，然后逐条断言
「该做的做了、不该做的没做」，尤其是这几条铁律：
  · 校验没过 / 状态不对 → 绝不启动做种、绝不留下"两头空"
  · 数据文件永不删除
  · qB 同 hash（站点只换 tracker 的常见情形）→ 删条目→加新→确认→不对就自动回灌
  · TR 校验期间先把下载限速压到 1KB/s（在 torrent-start 之前）

用法：  python3 tests/selftest.py         （全部）
        python3 tests/selftest.py QB-1 TR-2   （只跑指定的）
退出码 0 = 全过。
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import tracker_replace                                        # noqa: E402

QB_USER, QB_PW = "qb-user", "qb-test-pw"
TR_USER, TR_PW = "tr-user", "tr-test-pw"
OLD = "https://old-tracker.example/announce?passkey=OLDKEY"
NEW = "https://new.example.org/announce?passkey=NEWKEY"
DATA_DIR = "/data/pt"


# --------------------------------------------------------------- 造种子
def make_pair(name, diff_info=False):
    """返回 (旧.torrent, 新.torrent, 旧hash, 新hash)。
    diff_info=False：info 段一字不差 —— 站点只换了 tracker，新旧同 hash（现实最常见）
    diff_info=True ：站点重做过 info 段 —— 新旧 hash 不同"""
    def info(pl):
        return {b"piece length": pl, b"pieces": b"\0" * 40,
                b"name": name.encode("utf-8"), b"length": 4096}
    old = tracker_replace.bencode({b"announce": OLD.encode(), b"info": info(16384),
                           b"comment": b"site"})
    new = tracker_replace.bencode({b"announce": NEW.encode(), b"info": info(32768 if diff_info else 16384),
                           b"announce-list": [[NEW.encode()]], b"comment": b"site"})

    def h(raw):
        return hashlib.sha1(tracker_replace.bencode(tracker_replace.bdecode(raw)[0][b"info"])).hexdigest()
    return old, new, h(old), h(new)


def ih_of(raw):
    return hashlib.sha1(tracker_replace.bencode(tracker_replace.bdecode(raw)[0][b"info"])).hexdigest()


# --------------------------------------------------------------- 公共：仿真服务器
class Server(threading.Thread):
    daemon = True

    def __init__(self, app, tls=None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.handler())
        self.httpd.app = app
        self.port = self.httpd.server_address[1]
        app.port = self.port          # 让用例能从 mock 上拿到端口
        self.tls = tls                # (证书, 私钥) 有值就走 https
        super().__init__()

    def run(self):
        if self.tls:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.tls[0], self.tls[1])
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.httpd.serve_forever(poll_interval=0.05)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def parse_multipart(body, ctype):
    m = re.search(r'boundary="?([^";]+)"?', ctype or "")
    if not m:
        return {}, {}
    bnd = ("--" + m.group(1)).encode()
    fields, files = {}, {}
    for part in body.split(bnd):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        nm = re.search(rb'name="([^"]*)"', head)
        fn = re.search(rb'filename="([^"]*)"', head)
        if not nm:
            continue
        if fn:
            files[nm.group(1).decode()] = data
        else:
            fields[nm.group(1).decode()] = data.decode("utf-8", "replace")
    return fields, files


# --------------------------------------------------------------- 仿真 qBittorrent
class MockQB(object):
    """按 qB 4.6/5.0 的真实行为建模：私有种子碰到重复 hash → 直接回 Fails."""

    def __init__(self, torrent_raw, mode="ok", unwanted=False):
        self.mode = mode                       # ok backflow badtracker never addfail stuckold
        #                                addfail_newonly（新种子恒 Fails.） addrace（头两次 Fails. 后恢复）
        self.ih = ih_of(torrent_raw)
        self.calls = []                        # [(path, {参数})]
        self.entries = {}                      # hash -> dict
        self.entries[self.ih] = {
            "name": "Movie A", "save_path": DATA_DIR, "progress": 1.0,
            "state": "uploading", "downloaded": 0, "trackers": [OLD],
            "category": "pt", "tags": "oldtr", "size": 4096, "paused": False}
        self.unwanted = unwanted
        self.snapshot_at_add = None            # 加新那一刻列表里有谁

    def find(self, path):
        for p, args in reversed(self.calls):
            if p == path:
                return args
        return None

    def order(self, *paths):
        """断言用：这些写操作出现的先后顺序对不对"""
        idx = []
        for p in paths:
            hit = [i for i, (pp, _a) in enumerate(self.calls) if pp == p]
            if not hit:
                return False
            idx.append(hit[0] if p != "/delete" else hit[-1])
        return idx == sorted(idx)

    def handler(self):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):         # 静音
                pass

            def _send(self, code, body=b"", ctype="text/plain", extra=None):
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                app = self.server.app
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path.startswith("/api/") and self.headers.get("Cookie") != "SID=1":
                    return self._send(403, b"Forbidden")
                app.calls.append((u.path, q))
                if u.path == "/api/v2/app/version":
                    return self._send(200, b"v4.6.7")
                if u.path == "/api/v2/app/webapiVersion":
                    return self._send(200, b"2.9.2")
                if u.path == "/api/v2/torrents/info":
                    out = []
                    for h, e in app.entries.items():
                        out.append({"hash": h, "infohash_v1": h, "name": e["name"],
                                    "save_path": e["save_path"], "progress": e["progress"],
                                    "state": e["state"], "downloaded": e["downloaded"],
                                    "category": e["category"], "tags": e["tags"],
                                    "size": e["size"]})
                    return self._send(200, json.dumps(out), "application/json")
                if u.path == "/api/v2/torrents/trackers":
                    e = app.entries.get(q.get("hash"))
                    urls = ["** [DHT] **", "** [PeX] **"] + (e["trackers"] if e else [])
                    return self._send(200, json.dumps([{"url": x} for x in urls]),
                                      "application/json")
                if u.path == "/api/v2/torrents/properties":
                    e = app.entries.get(q.get("hash")) or {}
                    return self._send(200, json.dumps({"save_path": e.get("save_path", DATA_DIR),
                                                       "up_limit": -1, "down_limit": -1,
                                                       "ratio_limit": -2,
                                                       "seeding_time_limit": -2}),
                                      "application/json")
                if u.path == "/api/v2/torrents/files":
                    return self._send(200, json.dumps([
                        {"name": "Movie A/a.bin", "id": 0, "size": 4096,
                         "priority": 0 if app.unwanted else 1},
                        {"name": "Movie A/b.txt", "id": 1, "size": 10, "priority": 1}]),
                        "application/json")
                if u.path == "/api/v2/torrents/exportFile":
                    return self._send(200, app.old_raw or b"", "application/x-bittorrent")
                return self._send(404, b"nope")

            def do_POST(self):
                app = self.server.app
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type") or ""
                if u.path == "/api/v2/auth/login":
                    f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                    if f.get("username") != QB_USER or f.get("password") != QB_PW:
                        return self._send(403, b"Forbidden")
                    return self._send(200, b"Ok.", extra={"Set-Cookie": "SID=1; Path=/"})
                if u.path.startswith("/api/") and self.headers.get("Cookie") != "SID=1":
                    return self._send(403, b"Forbidden")
                if "multipart/form-data" in ctype:
                    fields, files = parse_multipart(body, ctype)
                else:
                    fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                    files = {}
                fields["_raw"] = files.get("torrents")
                app.calls.append((u.path, fields))
                if u.path == "/api/v2/torrents/add":
                    return self._send(200, app._add(app, fields))
                if u.path in ("/api/v2/torrents/resume", "/api/v2/torrents/pause"):
                    hs = json.loads(fields.get("hashes") or "[]")
                    for h in hs:
                        e = app.entries.get(h)
                        if not e:
                            continue
                        if u.path.endswith("resume"):
                            e["paused"] = False
                            e["state"] = "uploading" if e["progress"] >= 1.0 else "downloading"
                        else:
                            e["paused"] = True
                            e["state"] = "pausedUP" if e["progress"] >= 1.0 else "pausedDL"
                    return self._send(200, b"")
                if u.path == "/api/v2/torrents/delete":
                    if app.mode == "stuckold":
                        return self._send(200, b"")          # 模拟：条目赖着不走
                    for h in json.loads(fields.get("hashes") or "[]"):
                        app.entries.pop(h, None)
                    return self._send(200, b"")
                if u.path == "/api/v2/torrents/filePrio":
                    return self._send(200, b"")
                return self._send(404, b"nope")

        return H

    @staticmethod
    def _add(app, f):
        raw = f.get("_raw")
        if app.mode == "addfail" or not raw:
            app.last_add_body = b"Fails."
            return b"Fails."
        if app.mode == "addfail_newonly" and f.get("trackers"):
            app.last_add_body = b"Fails."       # 只拒"带新 tracker 的新增"，回灌旧的那次放行
            return b"Fails."
        h = ih_of(raw)
        if h in app.entries:                    # 真实 qB：私有种子重复 hash → 不加入
            app.last_add_body = b"Fails."
            return b"Fails."
        if app.mode == "addrace" and len([1 for q, _a in app.calls if q.endswith("/add")]) <= 2:
            app.last_add_body = b"Fails."       # 删条目滞后：头两次 hash 还被 qB 占着
            return b"Fails."
        if app.mode == "never" and len([1 for q, _a in app.calls if q.endswith("/add")]) <= 1:
            app.last_add_body = b"Ok."          # 只拦第一次，回灌那次放行
            return b"Ok."
        app.snapshot_at_add = list(app.entries.keys())
        paused = f.get("paused") == "true"
        skip = f.get("skip_checking") == "true"
        e = {"name": "Movie A", "save_path": f.get("savepath"), "progress": 1.0 if skip else 0.0,
             "state": ("pausedUP" if paused else "uploading") if skip
             else ("pausedDL" if paused else "checkingDL"),
             "downloaded": 0,
             "trackers": ([f["trackers"]] if f.get("trackers") else
                          (tracker_replace.torrent_info(raw)["announces"] or [NEW])),
             "category": f.get("category", ""),
             "tags": f.get("tags", ""), "size": 4096, "paused": paused, "add": f}
        if app.mode == "backflow":              # 模拟 skip_checking 失效（qB 5.3.0 bug）
            e["downloaded"] = 4321
            e["progress"] = 0.4
            e["state"] = "downloading"
            e["paused"] = False
        if app.mode == "badtracker":
            e["trackers"] = [OLD]
        app.entries[h] = e
        app.last_add_body = b"Ok."
        return b"Ok."


class MockTR(object):
    """按 Transmission RPC 建模：409 会话号、Basic 认证、TR4 的 arguments.session 嵌套、
    torrent-add 后跑一段脚本化的校验过程。"""

    def __init__(self, torrent_raw, script=None, mode="ok"):
        self.mode = mode                         # ok badauth
        self.ih = ih_of(torrent_raw)
        self.calls = []                          # [(method, arguments)]
        self.entries = {1: self._old(torrent_raw)}
        self.next_id = 2
        self.script = list(script or [])
        self.sid = "TRSID1234"
        self.got_sid = False

    def _old(self, raw):
        return {"id": 1, "name": "Movie A", "hashString": self.ih, "status": 6,
                "percentDone": 1.0, "recheckProgress": 0.0, "error": 0, "errorString": "",
                "downloadedEver": 0, "leftUntilDone": 0, "totalSize": 4096,
                "downloadDir": DATA_DIR, "labels": ["oldtr"], "isPrivate": True,
                "trackers": [{"id": 0, "announce": OLD}], "addedDate": 1,
                "files": [{"name": "a.bin", "length": 4096, "bytesCompleted": 4096,
                           "wantComplete": True}],
                "fileStats": [{"bytesCompleted": 4096, "wanted": False}],
                "ratioLimit": 3, "ratioMode": "single", "seedIdleLimit": 30,
                "seedIdleMode": "single", "uploadLimit": 512, "downloadLimit": 0,
                "peerLimit": 50}

    def call(self, method):
        for m, a in reversed(self.calls):
            if m == method:
                return a
        return None

    def order(self, *methods):
        idx = []
        for m in methods:
            hit = [i for i, (mm, _a) in enumerate(self.calls) if mm == m]
            if not hit:
                return False
            idx.append(hit[-1])
        return idx == sorted(idx)

    def handler(self):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                app = self.server.app
                if urlparse(self.path).path != "/transmission/rpc":
                    self.send_response(404)     # 让工具的"自动识别"能正确否定 qB
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode() or "{}")
                except ValueError:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                auth = self.headers.get("Authorization") or ""
                want = "Basic " + base64.b64encode(("%s:%s" % (TR_USER, TR_PW)).encode()).decode()
                if app.mode == "badauth" or auth != want:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="Transmission"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("X-Transmission-Session-Id") != app.sid:
                    self.send_response(409)
                    self.send_header("X-Transmission-Session-Id", app.sid)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                m = body.get("method")
                args = body.get("arguments") or {}
                app.calls.append((m, args))
                out = app.dispatch(m, args)
                payload = json.dumps({"result": "success", "arguments": out}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return H

    def dispatch(self, m, a):
        if m == "session-get":
            return {"session": {"version": "4.0.6", "rpc-version": 17,
                                "rpc-version-semantics": "x"}}
        if m == "torrent-get":
            if "ids" in a:
                tid = a["ids"][0] if isinstance(a["ids"], list) else a["ids"]
                e = self.entries.get(tid)
                if not e:
                    return {"torrents": []}
                e = dict(e)
                if tid != 1 and self.script:            # 新种子：按脚本走校验过程
                    e.update(self.script.pop(0) if len(self.script) > 1 else self.script[0])
                return {"torrents": [e]}
            return {"torrents": [dict(v) for v in self.entries.values()]}
        if m == "torrent-remove":
            for tid in a.get("ids", []):
                self.entries.pop(tid, None)
            return {}
        if m == "torrent-add":
            raw = base64.b64decode(a.get("metainfo") or "")
            tid = self.next_id
            self.next_id += 1
            trk = a.get("trackerAdd") or (tracker_replace.torrent_info(raw)["announces"] or [OLD])
            self.entries[tid] = {"id": tid, "name": "Movie A", "hashString": ih_of(raw),
                                 "status": 1, "percentDone": 0.0, "recheckProgress": 0.0,
                                 "error": 0, "errorString": "", "downloadedEver": 0,
                                 "leftUntilDone": 4096, "totalSize": 4096,
                                 "downloadDir": a.get("download-dir"), "labels": a.get("labels") or [],
                                 "isPrivate": True, "trackers": [{"id": 0, "announce": trk[0]}],
                                 "addedDate": 1, "files": [{"name": "a.bin", "length": 4096}],
                                 "fileStats": [{"bytesCompleted": 0, "wanted": True}],
                                 "ratioLimit": 0, "downloadLimit": 0, "downloadLimited": False,
                                 "_addargs": a}
            return {"torrent-added": {"id": tid, "name": "Movie A", "hashString": ih_of(raw)}}
        if m == "torrent-set":
            for tid in a.get("ids", []):
                if tid in self.entries:
                    self.entries[tid].update({k: v for k, v in a.items() if k != "ids"})
            return {}
        if m == "torrent-start":
            for tid in a.get("ids", []):
                if tid in self.entries:
                    self.entries[tid]["status"] = self.entries[tid].get("status", 4) or 4
            return {}
        if m == "torrent-stop":
            for tid in a.get("ids", []):
                if tid in self.entries:
                    self.entries[tid]["status"] = 0
            return {}
        return {}


# --------------------------------------------------------------- 跑一轮 tracker_replace
def run_tool(mock, kind, case, extra=(), new_raw=None, cfg_extra=None, yes=True,
             csv_map=None):
    work = os.path.join(HERE, "work", case)
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "newt"))
    shutil.copy(os.path.join(ROOT, "tracker_replace.py"), os.path.join(work, "tracker_replace.py"))
    open(os.path.join(work, "newt", "new.torrent"), "wb").write(new_raw)
    if csv_map:
        open(os.path.join(work, "map.csv"), "w", encoding="utf-8").write(
            "torrent,hash\nnewt/new.torrent,%s\n" % csv_map)
    user, pw = (QB_USER, QB_PW) if kind == "qb" else (TR_USER, TR_PW)
    cfg = {"instances": [{"name": "m", "kind": kind, "host": "127.0.0.1",
                          "port": mock.port, "user": user, "password": pw}]}
    if cfg_extra:
        cfg["instances"][0].update(cfg_extra)
        if cfg_extra.get("no_kind"):
            cfg["instances"][0].pop("kind")
    json.dump(cfg, open(os.path.join(work, "cfg.json"), "w"))
    cmd = [sys.executable, "tracker_replace.py", "apply", "-c", "cfg.json",
           "--old", "old-tracker.example", "--torrents-dir", "newt",
           "--sleep", "0", "--poll", "0.1", "--wait-limit", "2", "--max-fail", "0"]
    if csv_map:
        cmd += ["--csv", "map.csv"]
    if yes:
        cmd.append("--yes")
    cmd += list(extra)
    p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=420)
    if os.environ.get("TRREPLACE_DEBUG"):
        print("---- 工具输出 ----\n" + p.stdout + "\n---- 异常 ----\n" + p.stderr[-2000:])
    led = {}
    lp = os.path.join(work, "state", "ledger.json")
    if os.path.exists(lp):
        led = json.load(open(lp, encoding="utf-8"))
    return p, led, work


def done_of(led):
    for v in (led or {}).values():
        return v.get("done", {})
    return {}


def failed_of(led):
    for v in (led or {}).values():
        return v.get("failed", {})
    return {}


# --------------------------------------------------------------- 用例
RESULTS = []


def check(name, cond, note=""):
    RESULTS.append((name, bool(cond), note))
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("   " + note) if note else ""))
    return bool(cond)


def qb_case(case, mode, diff_info=False, unwanted=False, extra=(), yes=True):
    old, new, oh, nh = make_pair("Movie A", diff_info=diff_info)
    m = MockQB(old, mode=mode, unwanted=unwanted)   # 客户端里躺着的是旧种子
    m.old_raw = old
    s = Server(m)
    s.start()
    try:
        p, led, work = run_tool(m, "qb", case, extra=extra, new_raw=new, yes=yes,
                                csv_map=(oh if diff_info else None))
    finally:
        s.stop()
    return m, p, led, work, (oh, nh)


def tr_case(case, script, extra=(), yes=True, mode="ok"):
    old, new, oh, nh = make_pair("Movie A")
    m = MockTR(old, script=script, mode=mode)
    s = Server(m)
    s.start()
    try:
        p, led, work = run_tool(m, "tr", case, extra=extra, new_raw=new, yes=yes)
    finally:
        s.stop()
    return m, p, led, work, (oh, nh)


def main():
    only = set(sys.argv[1:])
    want = lambda c: (not only) or any(c.startswith(o) for o in only)   # noqa

    # ---------------- QB-1 同 hash 正常路径 ----------------
    if want("QB-1"):
        print("\nQB-1 qB 同 hash（站点只换 tracker）：删条目→加新→确认→做种")
        m, p, led, work, (oh, nh) = qb_case("QB-1", "ok")
        d = done_of(led)
        check("换成功且记账", oh in d, "%s" % list(d))
        check("skip_checking=true", (m.find("/api/v2/torrents/add") or {}).get("skip_checking") == "true")
        check("加入时是暂停态", (m.find("/api/v2/torrents/add") or {}).get("paused") == "true")
        check("先删旧条目再加新", m.order("/api/v2/torrents/delete", "/api/v2/torrents/add"))
        check("删除没动数据文件", (m.find("/api/v2/torrents/delete") or {}).get("deleteFiles") == "false")
        check("确认之后才启动做种", m.order("/api/v2/torrents/add", "/api/v2/torrents/resume"))
        e = list(m.entries.values())[0] if m.entries else {}
        check("最终 tracker 是新的", e.get("trackers") == [NEW], str(e.get("trackers")))
        check("最终在做种", e.get("state") == "uploading", str(e.get("state")))
        check("零下载", e.get("downloaded") == 0)

    # ---------------- QB-2 不同 hash 正常路径 ----------------
    if want("QB-2"):
        print("\nQB-2 qB 新旧 hash 不同：先加新，确认通过才删旧")
        m, p, led, work, (oh, nh) = qb_case("QB-2", "ok", diff_info=True)
        check("换成功且记账", oh in done_of(led), str(list(done_of(led))))
        check("加新时旧种子还在", oh in (m.snapshot_at_add or []), str(m.snapshot_at_add))
        check("先加新后删旧", m.order("/api/v2/torrents/add", "/api/v2/torrents/delete"))
        check("旧种子数据未删", (m.find("/api/v2/torrents/delete") or {}).get("deleteFiles") == "false")
        check("列表里只剩新种子", list(m.entries.keys()) == [nh], str(list(m.entries.keys())))

    # ---------------- QB-3 qB 拒收（回 Fails.）----------------
    if want("QB-3"):
        print("\nQB-3 qB 拒绝加入：必须失败收场，旧种子一根毛都不能动")
        m, p, led, work, (oh, nh) = qb_case("QB-3", "addfail", diff_info=True)
        check("结论是失败", p.returncode != 0 and not led.get("m", {}).get("done"), "rc=%s" % p.returncode)
        check("旧种子还在", oh in m.entries)
        check("没有删除操作", not any(x == "/api/v2/torrents/delete" for x, _a in m.calls))
        check("没有自动做种", not any(x == "/api/v2/torrents/resume" for x, _a in m.calls))

    # ---------------- QB-4 回流（skip_checking 失效）----------------
    if want("QB-4"):
        print("\nQB-4 新种子一加入就在下载 = skip_checking 没生效 → 撤销，旧种子不动")
        m, p, led, work, (oh, nh) = qb_case("QB-4", "backflow", diff_info=True)
        check("结论是失败", p.returncode != 0)
        check("旧种子还在", oh in m.entries)
        check("新种子被撤销", nh not in m.entries, str(list(m.entries.keys())))
        why = (failed_of(led).get(oh, {}) or {}).get("why", "")
        check("说清是回流", "回流" in why, why[:50])

    # ---------------- QB-5 tracker 不是新的 ----------------
    if want("QB-5"):
        print("\nQB-5 新种子的 tracker 不对 → 不许做种、不许删旧")
        m, p, led, work, (oh, nh) = qb_case("QB-5", "badtracker", diff_info=True)
        check("结论是失败", p.returncode != 0)
        check("旧种子还在", oh in m.entries)
        check("没有自动做种", not any(x == "/api/v2/torrents/resume" for x, _a in m.calls))

    # ---------------- QB-6 同 hash：新种子加不进来 → 自动回灌 ----------------
    if want("QB-6"):
        print("\nQB-6 同 hash 但新种子没能加进去 → 自动把旧条目加回继续做种")
        m, p, led, work, (oh, nh) = qb_case("QB-6", "never")
        check("结论是失败", p.returncode != 0)
        check("旧条目被回灌回来", oh in m.entries, str(list(m.entries.keys())))
        e = m.entries.get(oh) or {}
        check("回灌后 tracker 还是旧的", e.get("trackers") == [OLD], str(e.get("trackers")))
        check("回灌后继续做种", e.get("state") == "uploading", str(e.get("state")))

    # ---------------- QB-7 同 hash：旧条目赖着不走 ----------------
    if want("QB-7"):
        print("\nQB-7 同 hash 且旧条目删了还在（qB 正在卸载）→ 不硬来，回灌")
        m, p, led, work, (oh, nh) = qb_case("QB-7", "stuckold")
        check("结论是失败", p.returncode != 0)
        check("旧条目仍在", oh in m.entries)
        e = m.entries.get(oh) or {}
        check("tracker 仍是旧的", e.get("trackers") == [OLD], str(e.get("trackers")))
        check("没把新种子塞进来", sum(1 for x, _a in m.calls if x.endswith("/add")) <= 1,
              "add 调了 %d 次" % sum(1 for x, _a in m.calls if x.endswith("/add")))

    # ---------------- QB-8 演练 ----------------
    if want("QB-8"):
        print("\nQB-8 演练模式（不加 --yes）：一个写请求都不许发")
        m, p, led, work, (oh, nh) = qb_case("QB-8", "ok", yes=False)
        writes = [x for x, _a in m.calls if x.startswith("/api/v2/torrents/") and
                  x.split("/")[-1] in ("add", "delete", "resume", "pause", "filePrio")]
        check("零写操作", not writes, str(writes))
        check("旧种子还在", oh in m.entries)

    # ---------------- QB-9 登录失败 ----------------
    if want("QB-9"):
        print("\nQB-9 qB 密码错：友好报错，不做任何写操作")
        old, new, oh, nh = make_pair("Movie A")
        m = MockQB(old)
        m.old_raw = old
        s = Server(m)
        s.start()
        try:
            p, led, work = run_tool(m, "qb", "QB-9", new_raw=new,
                                    cfg_extra={"password": "wrong"})
        finally:
            s.stop()
        check("退出码非 0 或零处理", p.returncode != 0 or "连不上" in (p.stdout + p.stderr))
        check("报了登录失败", ("登录失败" in p.stdout + p.stderr) or ("连不上" in p.stdout + p.stderr))
        check("没发写请求", not any(x.endswith("/add") for x, _a in m.calls))

    # ---------------- QB-10 --pause 换完保持暂停 ----------------
    if want("QB-10"):
        print("\nQB-10 用户要求'换完先别做种'：确认通过后也不许启动")
        m, p, led, work, (oh, nh) = qb_case("QB-10", "ok", extra=["--pause"])
        check("换成功", oh in done_of(led))
        check("没有启动做种", not any(x == "/api/v2/torrents/resume" for x, _a in m.calls))
        e = list(m.entries.values())[0] if m.entries else {}
        check("保持暂停", str(e.get("state", "")).startswith("paused"), str(e.get("state")))

    # ---------------- QB-11 同 hash：200 Fails. 是删除滞后 → 重试救回 ----------------
    if want("QB-11"):
        print("\nQB-11 同 hash 加入头两次回 200 Fails.（hash 释放滞后）：重试应成功，别误判失败")
        m, p, led, work, (oh, nh) = qb_case("QB-11", "addrace", extra=["--add-gap", "0.1"])
        adds = sum(1 for x, _a in m.calls if x.endswith("/add"))
        check("最终成功", p.returncode == 0 and oh in done_of(led), "rc=%s" % p.returncode)
        check("add 自动重试过", adds >= 3, "add 调了 %d 次" % adds)
        e = m.entries.get(oh) or {}
        check("新 tracker 生效", e.get("trackers") == [NEW], str(e.get("trackers")))
        check("正常启动做种", e.get("state") == "uploading", str(e.get("state")))
        check("日志行里印了 hash 前缀", oh[:12] in p.stdout, p.stdout[-200:])

    # ---------------- QB-12 同 hash：qB 永久拒新种子 → 必须回灌 ----------------
    if want("QB-12"):
        print("\nQB-12 同 hash 新种子恒 Fails.：重试收场报错，旧条目回灌继续做种，不能两头空")
        m, p, led, work, (oh, nh) = qb_case("QB-12", "addfail_newonly",
                                            extra=["--add-tries", "2", "--add-gap", "0.1"])
        adds = sum(1 for x, _a in m.calls if x.endswith("/add"))
        check("结论是失败", p.returncode != 0 and not done_of(led).get(oh), "rc=%s" % p.returncode)
        check("add 重试过再放弃", adds >= 3, "add 调了 %d 次（2 次重试+1 次回灌）" % adds)
        check("旧条目回灌回来了", oh in m.entries, str(list(m.entries.keys())))
        e = m.entries.get(oh) or {}
        check("回灌用旧 tracker", e.get("trackers") == [OLD], str(e.get("trackers")))
        check("回灌后继续做种", e.get("state") == "uploading", str(e.get("state")))
        check("报错说清 Fails. 原因", "Fails." in (p.stdout + p.stderr))

    # ---------------- QB-13 用户根本不需要知道 hash ----------------
    if want("QB-13"):
        print("\nQB-13 失败日志必须印 hash；rollback 不带 --hash 列出候选、只给前缀也能回滚")
        m, p, led, work, (oh, nh) = qb_case("QB-13", "addfail_newonly",
                                            extra=["--add-tries", "2", "--add-gap", "0.1"])
        check("QB-13 失败行里印了 hash", oh[:12] in p.stdout, p.stdout[-200:])
        m.entries.pop(oh, None)          # 模拟最坏情况：连自动回灌也没成，条目彻底没了
        m.mode = "ok"                    # 用户拿到了能用的新种子，手动回滚
        s2 = Server(m)
        s2.start()
        try:
            time.sleep(0.2)
            cfg = json.load(open(os.path.join(work, "cfg.json")))
            cfg["instances"][0]["port"] = m.port
            json.dump(cfg, open(os.path.join(work, "cfg.json"), "w"))

            def rb(*args):
                r = subprocess.run([sys.executable, "tracker_replace.py", "rollback",
                                    "-c", "cfg.json"] + list(args),
                                   cwd=work, capture_output=True, text=True, timeout=60)
                return r, r.stdout + r.stderr
            r, out = rb()
            check("QB-13 不给 --hash 列出候选并含完整 hash",
                  r.returncode != 0 and oh in out, out[-200:])
            r, out = rb("--hash", "deadbeef")
            check("QB-13 前缀对不上时报错并重新列候选",
                  r.returncode != 0 and oh in out, out[-200:])
            r, out = rb("--hash", oh[:6])
            check("QB-13 只给 6 位 hash 前缀就完成回滚",
                  r.returncode == 0 and oh in m.entries
                  and m.entries[oh].get("trackers") == [NEW], out[-200:])
        finally:
            s2.stop()

    # ---------------- QB-14 recover 找回丢掉的条目 ----------------
    if want("QB-14"):
        print("\nQB-14 recover：列表里没了对应的 hash → 按名字+大小找到数据 → 找回做种，只加不删")
        m, p, led, work, (oh, nh) = qb_case("QB-14", "ok")
        m.entries.clear()                    # 模拟两头空现场：列表空了，数据还在盘上
        dd = os.path.join(work, "datax")
        os.makedirs(dd, exist_ok=True)
        open(os.path.join(dd, "Movie A"), "wb").write(b"\0" * 4096)
        s2 = Server(m)
        s2.start()
        try:
            time.sleep(0.2)
            cfg = json.load(open(os.path.join(work, "cfg.json")))
            cfg["instances"][0]["port"] = m.port
            json.dump(cfg, open(os.path.join(work, "cfg.json"), "w"))
            r = subprocess.run([sys.executable, "tracker_replace.py", "recover",
                                "-c", "cfg.json", "--torrents-dir", "newt",
                                "--root", "datax"],
                               cwd=work, capture_output=True, text=True, timeout=120)
            out = r.stdout + r.stderr
            e = m.entries.get(oh) or {}
            check("QB-14 recover 把丢的条目加回来了",
                  r.returncode == 0 and oh in m.entries, out[-200:])
            check("QB-14 认出数据并启动做种", e.get("state") == "uploading", str(e.get("state")))
            check("QB-14 零回流没重下", e.get("downloaded") == 0, str(e.get("downloaded")))
            check("QB-14 加回了原数据所在目录", e.get("save_path") == "datax", str(e.get("save_path")))
            # 再跑一次：已存在的不能再被折腾（幂等）
            r2 = subprocess.run([sys.executable, "tracker_replace.py", "recover",
                                 "-c", "cfg.json", "--torrents-dir", "newt",
                                 "--root", "datax"],
                                cwd=work, capture_output=True, text=True, timeout=120)
            check("QB-14 第二次跑报缺 0 份（幂等）",
                  r2.returncode == 0 and "缺 0 份" in r2.stdout, (r2.stdout + r2.stderr)[-160:])
        finally:
            s2.stop()

    # ---------------- QB-15 repair 一键修复（备份在旧版文件夹） ----------------
    if want("QB-15"):
        print("\nQB-15 repair：新版是全新文件夹、备份躺在旁边旧版文件夹——一键把丢的条目修回来")
        m, p, led, work, (oh, nh) = qb_case("QB-15", "ok")
        P = tempfile.mkdtemp(prefix="QB15")
        old_dir = os.path.join(P, "oldver")       # 旧版文件夹：state/backup 在里头
        new_dir = os.path.join(P, "newver")       # 新版文件夹：只有脚本，state 空
        os.makedirs(new_dir)
        shutil.copytree(os.path.join(work, "state"), os.path.join(old_dir, "state"))
        shutil.copy(os.path.join(ROOT, "tracker_replace.py"), new_dir)
        m.entries.clear()                         # 条目没了的最坏现场，数据仍在盘上
        s2 = Server(m)
        s2.start()
        try:
            time.sleep(0.2)
            cfg = {"instances": [{"name": "m", "kind": "qb", "host": "127.0.0.1",
                                  "port": m.port, "user": QB_USER, "password": QB_PW}]}
            json.dump(cfg, open(os.path.join(new_dir, "cfg.json"), "w"))
            r = subprocess.run([sys.executable, "tracker_replace.py", "repair",
                                "-c", "cfg.json"],
                               cwd=new_dir, capture_output=True, text=True, timeout=120)
            out = r.stdout + r.stderr
            e = m.entries.get(oh) or {}
            check("QB-15 自动找到旁边的旧版备份", "自动找到备份目录" in out, out[-220:])
            check("QB-15 一键修复把条目加回", r.returncode == 0 and oh in m.entries, out[-220:])
            check("QB-15 认全后恢复做种且零回流",
                  e.get("state") == "uploading" and e.get("downloaded") == 0, str(e)[:200])
            check("QB-15 修复用的是新 tracker",
                  m.entries.get(oh, {}).get("trackers") == [NEW],
                  str(m.entries.get(oh, {}).get("trackers")))
            r2 = subprocess.run([sys.executable, "tracker_replace.py", "repair",
                                 "-c", "cfg.json"], cwd=new_dir,
                                capture_output=True, text=True, timeout=120)
            check("QB-15 再跑修复 0 份（幂等）",
                  r2.returncode == 0 and "缺 0 份" in r2.stdout, (r2.stdout + r2.stderr)[-160:])
            # 新旧文件夹不挨着：--state-dir  explicit 指过去
            m.entries.clear()
            far = tempfile.mkdtemp(prefix="QB15FAR")
            shutil.copy(os.path.join(ROOT, "tracker_replace.py"), far)
            json.dump(cfg, open(os.path.join(far, "cfg.json"), "w"))
            r3 = subprocess.run([sys.executable, "tracker_replace.py", "repair",
                                 "-c", "cfg.json", "--state-dir",
                                 os.path.join(old_dir, "state")],
                                cwd=far, capture_output=True, text=True, timeout=120)
            check("QB-15 --state-dir 指旧文件夹同样能修",
                  r3.returncode == 0 and oh in m.entries, (r3.stdout + r3.stderr)[-200:])
        finally:
            s2.stop()

    # ---------------- QB-16 rollback --pick-state（旧备份放在远处） ----------------    if want("QB-16"):
        print("\nQB-16 --pick-state：旧版工具文件夹放在八竿子打不着的地方，回滚照样找得到")
        m, p, led, work, (oh, nh) = qb_case("QB-16", "ok")
        P = tempfile.mkdtemp(prefix="QB16")
        far = os.path.join(P, "somewhere", "oldtool")   # 跟新版完全不挨着
        os.makedirs(far)
        shutil.copytree(os.path.join(work, "state"), os.path.join(far, "state"))
        open(os.path.join(far, "oldtool.exe"), "wb").write(b"MZ")  # 允许直接指旧 exe
        new_dir = os.path.join(P, "newver")
        os.makedirs(new_dir)
        shutil.copy(os.path.join(ROOT, "tracker_replace.py"), new_dir)
        m.entries.clear()
        s2 = Server(m)
        s2.start()
        try:
            time.sleep(0.2)
            cfg = {"instances": [{"name": "m", "kind": "qb", "host": "127.0.0.1",
                                  "port": m.port, "user": QB_USER, "password": QB_PW}]}
            json.dump(cfg, open(os.path.join(new_dir, "cfg.json"), "w"))
            r0 = subprocess.run([sys.executable, "tracker_replace.py", "rollback",
                                 "-c", "cfg.json"], cwd=new_dir,
                                capture_output=True, text=True, timeout=60)
            check("QB-16 不指目录时列表里没有远处的备份", oh not in (r0.stdout + r0.stderr),
                  (r0.stdout + r0.stderr)[-160:])
            r1 = subprocess.run([sys.executable, "tracker_replace.py", "rollback",
                                 "-c", "cfg.json", "--pick-state", far],
                                cwd=new_dir, capture_output=True, text=True, timeout=60)
            check("QB-16 指旧文件夹后备份进了列表", oh in (r1.stdout + r1.stderr),
                  (r1.stdout + r1.stderr)[-200:])
            r2 = subprocess.run([sys.executable, "tracker_replace.py", "rollback",
                                 "--hash", oh[:8], "-c", "cfg.json",
                                 "--pick-state", os.path.join(far, "oldtool.exe")],
                                cwd=new_dir, capture_output=True, text=True, timeout=120)
            check("QB-16 直接指旧 exe 也能按前缀回滚",
                  r2.returncode == 0 and oh in m.entries, (r2.stdout + r2.stderr)[-200:])
        finally:
            s2.stop()

    # ---------------- QB-17 import 一键导入旧版备份/实例/台账 ----------------
    if want("QB-17"):
        print("\nQB-17 import：旧版整个文件夹的家当（备份+客户端+台账）一键并进新版，导完不用再指旧目录")
        P = tempfile.mkdtemp(prefix="QB17")
        old = os.path.join(P, "oldtool")
        new = os.path.join(P, "newtool")
        os.makedirs(os.path.join(old, "state", "backup", "127.0.0.1_9999"))
        os.makedirs(new)
        h = "ab" * 20
        open(os.path.join(old, "state", "backup", "127.0.0.1_9999", h + ".new.torrent"),
             "wb").write(b"TOR")
        json.dump({"name": "老片子", "path": "/data"},
                  open(os.path.join(old, "state", "backup", "127.0.0.1_9999",
                                    h + ".state.json"), "w"))
        json.dump({"instances": [{"kind": "qb", "scheme": "http", "host": "10.9.9.9",
                                  "port": 8080, "user": "u", "password": "p"}]},
                  open(os.path.join(old, "guiconfig.json"), "w"))
        json.dump({"127.0.0.1_9999": {"done": {h: {"name": "老片子", "ts": "t"}}}},
                  open(os.path.join(old, "state", "ledger.json"), "w"))
        shutil.copy(os.path.join(ROOT, "tracker_replace.py"), new)
        r = subprocess.run([sys.executable, "tracker_replace.py", "import",
                            "--from", old, "--dry-run"], cwd=new,
                         capture_output=True, text=True, timeout=60)
        check("QB-17 演练报数且不落盘",
              r.returncode == 0 and "备份 +2" in r.stdout
              and not os.path.exists(os.path.join(new, "state", "backup")),
              (r.stdout + r.stderr)[-200:])
        r2 = subprocess.run([sys.executable, "tracker_replace.py", "import", "--from", old],
                            cwd=new, capture_output=True, text=True, timeout=60)
        bf = os.path.join(new, "state", "backup", "127.0.0.1_9999", h + ".new.torrent")
        gu = json.load(open(os.path.join(new, "guiconfig.json"), encoding="utf-8"))
        ld = json.load(open(os.path.join(new, "state", "ledger.json"), encoding="utf-8"))
        check("QB-17 备份真进了新版 state", r2.returncode == 0 and os.path.exists(bf),
              (r2.stdout + r2.stderr)[-200:])
        check("QB-17 旧版实例并进 guiconfig",
              any(i.get("host") == "10.9.9.9" for i in gu.get("instances", [])), str(gu)[:200])
        check("QB-17 旧版台账并进 ledger", h in (ld.get("127.0.0.1_9999", {}).get("done", {})),
              str(ld)[:200])
        check("QB-17 旧版文件原样没动",
              os.path.exists(os.path.join(old, "state", "backup", "127.0.0.1_9999",
                                          h + ".new.torrent")), "旧版被动过")
        r3 = subprocess.run([sys.executable, "tracker_replace.py", "import", "--from", old],
                            cwd=new, capture_output=True, text=True, timeout=60)
        check("QB-17 再导一次幂等（全是跳过）", "备份 +0" in r3.stdout and "跳过 2" in r3.stdout,
              (r3.stdout + r3.stderr)[-160:])
        r4 = subprocess.run([sys.executable, "tracker_replace.py", "rollback",
                             "-c", "cfg_none.json"], cwd=new,
                            capture_output=True, text=True, timeout=60)
        check("QB-17 导完列表直接可见，不再需要 --pick-state",
              h in (r4.stdout + r4.stderr), (r4.stdout + r4.stderr)[-160:])

    # ---------------- QB-18 apply 开工自检：自动找回旧版误删的条目 ----------------
    if want("QB-18"):
        print("\nQB-18 apply：干活前发现旧版「两头空」误删的条目，先自动找回再开工")
        m, p, led, work, (oh, nh) = qb_case("QB-18", "ok")
        del m.entries[oh]                       # 干完活后条目被旧版误删的最坏现场
        s2 = Server(m)
        s2.start()
        try:
            time.sleep(0.2)
            cfg = {"instances": [{"name": "m", "kind": "qb", "host": "127.0.0.1",
                                  "port": m.port, "user": QB_USER, "password": QB_PW}]}
            json.dump(cfg, open(os.path.join(work, "cfg.json"), "w"))
            r = subprocess.run(
                [sys.executable, "tracker_replace.py", "apply", "-c", "cfg.json",
                 "--old", "old-tracker.example", "--torrents-dir", "newt",
                 "--sleep", "0", "--poll", "0.1", "--wait-limit", "2",
                 "--max-fail", "0", "--yes"],
                cwd=work, capture_output=True, text=True, timeout=240)
            out = r.stdout + r.stderr
            e = m.entries.get(oh) or {}
            check("QB-18 开工前报出误删并自动找回",
                  r.returncode == 0 and "自动找回" in out and oh in m.entries, out[-260:])
            check("QB-18 找回后认全数据恢复做种且零回流",
                  e.get("state") == "uploading" and e.get("downloaded") == 0, str(e)[:200])
            check("QB-18 找回用新 tracker",
                  (m.entries.get(oh) or {}).get("trackers") == [NEW],
                  str((m.entries.get(oh) or {}).get("trackers")))
            r2 = subprocess.run(
                [sys.executable, "tracker_replace.py", "apply", "-c", "cfg.json",
                 "--old", "old-tracker.example", "--torrents-dir", "newt",
                 "--sleep", "0", "--poll", "0.1", "--wait-limit", "2",
                 "--max-fail", "0", "--yes"],
                cwd=work, capture_output=True, text=True, timeout=240)
            check("QB-18 没丢东西时不多嘴、不误动",
                  "自动找回" not in (r2.stdout + r2.stderr),
                  (r2.stdout + r2.stderr)[-160:])
            del m.entries[oh]                   # 再演一次误删，这回用 --no-auto-fix 关掉
            r3 = subprocess.run(
                [sys.executable, "tracker_replace.py", "apply", "-c", "cfg.json",
                 "--old", "old-tracker.example", "--torrents-dir", "newt",
                 "--sleep", "0", "--poll", "0.1", "--wait-limit", "2",
                 "--max-fail", "0", "--yes", "--no-auto-fix"],
                cwd=work, capture_output=True, text=True, timeout=240)
            check("QB-18 --no-auto-fix 时只报不动手",
                  "--no-auto-fix" in (r3.stdout + r3.stderr) and oh not in m.entries,
                  (r3.stdout + r3.stderr)[-200:])
        finally:
            s2.stop()

    # ---------------- TR-1 校验通过 ----------------
    if want("TR-1"):
        print("\nTR-1 Transmission 校验通过：限速在启动之前、数据不删、通过才做种")
        script = [{"recheckProgress": 0.4, "status": 4, "percentDone": 0.0, "leftUntilDone": 4096},
                  {"recheckProgress": 1.0, "status": 6, "percentDone": 1.0, "leftUntilDone": 0}]
        m, p, led, work, (oh, nh) = tr_case("TR-1", script, extra=["--check-budget", "30"])
        new_id = 2
        check("换成功且记账", oh in done_of(led), p.stdout[-200:])
        check("先 409 再拿会话号重发", any(x == "session-get" for x, _a in m.calls))
        check("删条目但保留数据", (m.call("torrent-remove") or {}).get("delete-local-data") is False)
        aa = (m.entries.get(new_id) or {}).get("_addargs") or {}
        check("新种子暂停态加入", aa.get("paused") is True)
        check("保留 labels", aa.get("labels") == ["oldtr"], str(aa.get("labels")))
        check("保留'不下载'的文件勾选", aa.get("files_unwanted") == [0], str(aa.get("files_unwanted")))
        caps = [i for i, (mm, aa) in enumerate(m.calls)
                if mm == "torrent-set" and aa.get("downloadLimited") and aa.get("downloadLimit") == 1]
        starts = [i for i, (mm, _a) in enumerate(m.calls) if mm == "torrent-start"]
        check("校验期间压低下载限速(1KB/s)", bool(caps))
        check("限速发生在 torrent-start 之前", bool(caps and starts) and min(caps) < min(starts),
              "cap@%s start@%s" % (caps[:1], starts[:1]))
        check("结束后恢复限速", any(mm == "torrent-set" and aa.get("downloadLimit") == 0
                                    for mm, aa in m.calls[caps[0]:]) if caps else False)
        check("结束后仍在做种（没被停）", (m.entries.get(new_id) or {}).get("status") != 0,
              str((m.entries.get(new_id) or {}).get("status")))
        check("零下载", (m.entries.get(new_id) or {}).get("downloadedEver") == 0)

    # ---------------- TR-2 校验未通过 ----------------
    if want("TR-2"):
        print("\nTR-2 校验跑完但盘上有缺口 → 停住保持暂停，绝不下载")
        script = [{"recheckProgress": 0.9, "status": 4, "percentDone": 0.0, "leftUntilDone": 4096},
                  {"recheckProgress": 1.0, "status": 4, "percentDone": 0.85, "leftUntilDone": 600}]
        m, p, led, work, (oh, nh) = tr_case("TR-2", script, extra=["--check-budget", "30"])
        f = failed_of(led).get(oh, {})
        check("结论=校验未通过", "校验未通过" in f.get("why", "") or "校验未通过" in p.stdout,
              f.get("why", "")[:60])
        check("已停住（torrent-stop）", any(x == "torrent-stop" for x, _a in m.calls))
        check("条目状态=停止", (m.entries.get(2) or {}).get("status") == 0,
              str((m.entries.get(2) or {}).get("status")))
        check("一个字节都没下", (m.entries.get(2) or {}).get("downloadedEver") == 0)
        check("没记成成功", oh not in done_of(led))

    # ---------------- TR-3 回流 ----------------
    if want("TR-3"):
        print("\nTR-3 校验中途真的开始下载 → 立刻停住")
        script = [{"recheckProgress": 0.2, "status": 4, "percentDone": 0.5,
                   "leftUntilDone": 2000, "downloadedEver": 7000}]
        m, p, led, work, (oh, nh) = tr_case("TR-3", script, extra=["--check-budget", "30"])
        check("结论=回流", "回流" in (failed_of(led).get(oh, {}).get("why", "") + p.stdout))
        check("已停住", any(x == "torrent-stop" for x, _a in m.calls))

    # ---------------- TR-4 校验超时 ----------------
    if want("TR-4"):
        print("\nTR-4 校验迟迟不结束 → 到点停住，不留在可能下载的状态")
        script = [{"recheckProgress": 0.1, "status": 4, "percentDone": 0.0, "leftUntilDone": 4096}]
        m, p, led, work, (oh, nh) = tr_case("TR-4", script, extra=["--check-budget", "3"])
        check("结论=校验超时", ("秒还没跑完" in p.stdout) or ("未就绪" in p.stdout)
              or ("校验超时" in p.stdout), p.stdout[-120:])
        check("已停住", any(x == "torrent-stop" for x, _a in m.calls))
        check("没记成成功", oh not in done_of(led))

    # ---------------- TR-5 --pause ----------------
    if want("TR-5"):
        print("\nTR-5 校验通过但用户要求先别做种 → 停住等人工确认")
        script = [{"recheckProgress": 1.0, "status": 6, "percentDone": 1.0, "leftUntilDone": 0}]
        m, p, led, work, (oh, nh) = tr_case("TR-5", script, extra=["--check-budget", "30", "--pause"])
        check("换成功", oh in done_of(led))
        check("结束后是停止态", (m.entries.get(2) or {}).get("status") == 0,
              str((m.entries.get(2) or {}).get("status")))

    # ---------------- TR-6 认证失败 ----------------
    if want("TR-6"):
        print("\nTR-6 TR 密码错（401）：报错退出，零写操作")
        old, new, oh, nh = make_pair("Movie A")
        m = MockTR(old, mode="badauth")
        s = Server(m)
        s.start()
        try:
            p, led, work = run_tool(m, "tr", "TR-6", new_raw=new, cfg_extra={"password": "x"})
        finally:
            s.stop()
        writes = [x for x, _a in m.calls if x.startswith("torrent-") and x != "torrent-get"]
        check("零写操作", not writes, str(writes))
        check("说了连不上/认证问题", ("连不上" in p.stdout + p.stderr) or ("401" in p.stdout + p.stderr),
              (p.stdout + p.stderr)[:100])

    # ---------------- TR-7 配不到新种子文件 ----------------
    if want("TR-7"):
        print("\nTR-7 目录里没有对应的新 .torrent → 拒动，不许猜")
        old, new, oh, nh = make_pair("Movie A")
        other, new2, oh2, nh2 = make_pair("Movie Z")
        m = MockTR(old, script=[{"recheckProgress": 1.0, "status": 6, "percentDone": 1.0}])
        s = Server(m)
        s.start()
        try:
            p, led, work = run_tool(m, "tr", "TR-7", new_raw=new2)     # 放一份 hash 对不上的
        finally:
            s.stop()
        why = failed_of(led).get(oh, {}).get("why", "")
        check("结论=配不到新种子", ("配不到" in why) or ("配不到" in p.stdout), why[:60])
        check("零写操作", not [x for x, _a in m.calls if x in ("torrent-remove", "torrent-add")])

    # ---------------- GEN-1 幂等：跑两遍 ----------------
    if want("GEN-1"):
        print("\nGEN-1 同一个 client 跑两遍：第二遍必须 0 待处理（断点续跑不重复折腾）")
        script = [{"recheckProgress": 1.0, "status": 6, "percentDone": 1.0, "leftUntilDone": 0}]
        old, new, oh, nh = make_pair("Movie A")
        m = MockTR(old, script=script)
        s = Server(m)
        s.start()
        try:
            p1, led1, work = run_tool(m, "tr", "GEN-1", new_raw=new, extra=["--check-budget", "30"])
            # 第二次：复用同一个 work（台账在）与同一个 mock
            cmd = [sys.executable, "tracker_replace.py", "apply", "-c", "cfg.json", "--old", "old-tracker.example",
                   "--torrents-dir", "newt", "--yes", "--sleep", "0", "--poll", "0.1",
                   "--check-budget", "30", "--wait-limit", "2"]
            p2 = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=300)
        finally:
            s.stop()
        check("第一遍换成功", oh in done_of(led1))
        check("第二遍 0 待处理", ("待处理 0" in p2.stdout), p2.stdout[-160:])

    # ---------------- GEN-2 自动识别客户端 + doctor ----------------
    if want("GEN-2"):
        print("\nGEN-2 不写 kind：工具自己认出是 TR（含 TR4 的 arguments.session 嵌套）")
        old, new, oh, nh = make_pair("Movie A")
        m = MockTR(old, script=[{"recheckProgress": 1.0, "status": 6, "percentDone": 1.0}])
        s = Server(m)
        s.start()
        work = os.path.join(HERE, "work", "GEN-2")
        if os.path.exists(work):
            shutil.rmtree(work)
        os.makedirs(work)
        shutil.copy(os.path.join(ROOT, "tracker_replace.py"), os.path.join(work, "tracker_replace.py"))
        json.dump({"instances": [{"name": "m", "host": "127.0.0.1", "port": m.port,
                                  "user": TR_USER, "password": TR_PW}]},
                  open(os.path.join(work, "cfg.json"), "w"))
        p = subprocess.run([sys.executable, "tracker_replace.py", "doctor", "-c", "cfg.json",
                            "--old", "old-tracker.example"], cwd=work, capture_output=True, text=True,
                         timeout=200)
        s.stop()
        check("认出 Transmission", "Transmission 4.0.6" in p.stdout, p.stdout[-200:])
        check("说清不能跳校验", "跳过校验" in p.stdout or "重校验" in p.stdout)

    # ---------------- PROTO：http / https 协议 ----------------
    if want("PROTO"):
        print("\nPROTO 协议：https 走 TLS；自签证书只有勾了「跳过证书校验」才连得上")
        check("_StdClient 默认不跳过证书校验", tracker_replace._StdClient().insecure is False)
        check("insecure 参数能设上", tracker_replace._StdClient(insecure=True).insecure is True)
        crt = os.path.join(HERE, "work", "tls.crt")
        key = os.path.join(HERE, "work", "tls.key")
        if not os.path.isdir(os.path.join(HERE, "work")):
            os.makedirs(os.path.join(HERE, "work"))
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", key, "-out", crt, "-days", "2",
                            "-subj", "/CN=localhost"],
                           check=True, capture_output=True, timeout=120)
        except Exception as e:
            print("  （本机没有 openssl，跳过 https 实测：%s）" % str(e)[:60])
            check("无 openssl 时也能跑（用例自动跳过）", True)
        else:
            old, new, oh, nh = make_pair("Movie A")
            m = MockQB(old, mode="ok", unwanted=False)
            s = Server(m, tls=(crt, key))
            s.start()
            try:
                c = tracker_replace.QbClient("127.0.0.1", s.port, QB_USER, QB_PW,
                                     scheme="https", insecure=True)
                check("https+跳过校验：登录成功、地址带协议",
                      c.base == "https://127.0.0.1:%d" % s.port and bool(c.version))
                c2 = tracker_replace.connect({"host": "127.0.0.1", "port": s.port, "kind": "qb",
                                      "user": QB_USER, "password": QB_PW,
                                      "scheme": "https", "insecure": True})
                check("config 里的 scheme/insecure 会被用上",
                      c2.kind == "qb" and c2.base.startswith("https://"))
                try:
                    tracker_replace.QbClient("127.0.0.1", s.port, QB_USER, QB_PW,
                                     scheme="https", insecure=False)
                    check("https 开证书校验：自签证书连不上（不会假装成功）", False, "居然连上了")
                except Exception:
                    check("https 开证书校验：自签证书连不上（不会假装成功）", True)
                try:
                    tracker_replace.QbClient("127.0.0.1", s.port, QB_USER, QB_PW, scheme="http")
                    check("拿 http 连 https 端口：直接报错，不乱猜协议", False, "居然连上了")
                except Exception:
                    check("拿 http 连 https 端口：直接报错，不乱猜协议", True)
            finally:
                s.stop()

    # ---------------- VER：版本号 / 界面按钮 ----------------
    if want("VER"):
        print("\nVER 版本号与界面按钮")
        check("有版本号、形如 x.y", bool(re.match(r"^\d+\.\d+", tracker_replace.VERSION)),
              tracker_replace.VERSION)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tracker_replace.py"), "--version"],
                           capture_output=True, text=True, timeout=90)
        check("--version 打得出来", tracker_replace.VERSION in p.stdout, p.stdout.strip()[:60])
        us = open(os.path.join(ROOT, "tracker_ui.py"), encoding="utf-8").read()
        line_of_title = [l for l in us.splitlines() if "root.title(" in l][0]
        check("原生窗口标题带版本号", ("tracker替换" in line_of_title and "tracker_replace.VERSION" in line_of_title))
        check("网页窗口显示版本号", 'id="ver"' in us and '"version": tracker_replace.VERSION' in us)
        # 公开版：代码与文档里都不许出现旧名（检查代码自身也不写字面量）
        _oldname = "pig" + "tool"
        for _f in ("tracker_ui.py", "tracker_replace.py", "README.md", "RUNME.txt",
                   "WINDOWS.txt", "build_linux.sh", "build_windows.bat",
                   "config.example.json", "requirements.txt", "tracker-replace.sh",
                   "tracker-replace-gui.bat", "tracker-replace-ui.bat"):
            _p = os.path.join(ROOT, _f)
            if not os.path.exists(_p):
                continue
            check("旧名不残留在 " + _f,
                  _oldname not in open(_p, encoding="utf-8").read().lower())
        check("原生窗口有清空日志按钮", us.count("清空日志") >= 2)
        check("网页窗口有清空日志按钮", "clearLog()" in us)

    # ---------------- 汇总 ----------------
    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 62)
    print("断言 %d 条：通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for nm, _c, note in bad:
        print("  ✗ %s  %s" % (nm, note))
    print("=" * 62)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
