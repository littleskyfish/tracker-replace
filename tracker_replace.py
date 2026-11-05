#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tracker替换 —— 批量替换 tracker（qBittorrent / Transmission 通吃）

为什么必须"加新种子 → 确认 → 删旧种子"，而不是原地改 tracker：
站点换 tracker 时发的是**另一份 .torrent 文件**，announce-list、created by、comment
都可能不同，甚至 info 段也重做过 —— 拿旧种子改个 announce 凑出来的那份，站点不认，
也不能算作原种子的延续。所以本工具只提供这一条路径，两个客户端都一样：

    下载好的新 .torrent → 按 info hash 配对 → 加入 → 确认做种且零回流 → 才删旧种子

两个客户端在"别把数据重新下一遍"上的能力差别很大：

  * qBittorrent：POST /torrents/add 支持 skip_checking=true（自 v3.3.13/2017 起所有版本），
    加入后直接认盘上已有数据、不做哈希校验 —— 实测几十 GB 的种子瞬时 100%、零回流。
    已知 qBittorrent 5.3.0 有忽略该参数的 bug，工具会检测到并撤销新种子。
  * Transmission：**RPC 任何版本都没有跳过校验的参数**。torrent_add 的全部参数见官方
    docs/rpc-spec.md：cookies / download_dir / filename / labels / metainfo / paused /
    peer_limit / bandwidth_priority / files_wanted / files_unwanted / priority_high /
    priority_low / priority_normal / sequential_download / sequential_download_from_piece
    —— 没有 seed_mode 之类的开关（libtorrent 的 seed_mode 从未暴露到 RPC）。
    并且私有种子禁止改 tracker 列表（torrent-set 直接报 error setting announce list），
    同 hash 也不能并存，所以只能"先删后加"。本工具做的是带零回流守护的重校验：
    先存快照与 .torrent 备份 → 删条目保留数据 → 暂停态加入（保留文件勾选）→ 启动让它
    读盘校验 → 死盯 downloadedEver，一旦真的开始下载立刻停手，绝不让它重下一遍。


安全设计（写死在代码里）：
  1 默认演练（dry-run），必须 --yes 才动手
  2 数据文件永不删除（qB deleteFiles=false / TR delete-local-data=false）
  3 动手前必备份 .torrent 与状态快照到 state/，失败可用 rollback 子命令还原
  4 只处理 tracker 命中 --old 的种子；新 tracker 已生效的直接跳过（幂等，可断点续跑）
  5 新种子未确认「做种中 + tracker 正确 + 零回流」之前，绝不删旧种子（qB 路线）
  6 TR 路线被迫先删后加，因此备份失败 / 目录取不到 / 文件数异常 → 一律拒动
  7 全程留痕：state/ledger.json（台账）、results.csv（逐条结果）、tracker-replace.log

依赖：Python 3.7+ 标准库，零第三方依赖（bencode、HTTP 客户端都内置）。
界面：gui 子命令＝原生窗口（需 tkinter）；ui 子命令＝本地网页窗口（免 tkinter，
      不装任何东西也能用）；两者都不改安全规则，只是换个按扭。
配置：不含任何地址/账号，见 config.example.json；密码可用环境变量 TRREPLACE_PASSWORD。
"""
import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import time
import http.client
import binascii
from datetime import datetime
from urllib.parse import urlencode, urlparse

VERSION = "1.9"        # 每次改动往上加：--version、窗口标题、压缩包名都用它

def _base_dir():
    """程序所在目录。PyInstaller 单文件模式下 __file__ 指向退出就销毁的临时解压
    目录，state/backup 落在那儿会随程序退出一起消失——回滚就没得回了，所以必须
    用可执行文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _base_dir()
ST_DIR = os.path.join(HERE, "state")
LEDGER = os.path.join(ST_DIR, "ledger.json")
LOGF = os.path.join(HERE, "tracker-replace.log")
RESULTS = os.path.join(HERE, "results.csv")

# Transmission status 码：0停 1等校验 2校验中 3等下载 4下载中 5等做种 6做种中
TR_STOPPED, TR_CWAIT, TR_CHECK, TR_DWAIT, TR_DL, TR_SWAIT, TR_SEED = range(7)
TR_CHECKING = (TR_CWAIT, TR_CHECK)
# qBittorrent 视为"已可上报做种"的状态
QB_UP = ("uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP")
QB_DL = ("downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL",
         "allocating", "moving", "checkingDL", "queuedDL")


# ----------------------------------------------------------------- 小工具
_SINK = None


def set_logger(fn):
    """图形界面用它接管日志；fn(text) 可能来自非主线程。"""
    global _SINK
    _SINK = fn


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    fn = _SINK
    if fn is not None:
        try:
            fn(line)
        except Exception:
            print(line, flush=True)
    else:
        print(line, flush=True)
    try:
        os.makedirs(HERE, exist_ok=True)
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def die(msg, code=2):
    print("\n" + "=" * 60 + "\n[停止] " + msg + "\n" + "=" * 60)
    sys.exit(code)


# ------------------------------------------------------- bencode（内置实现）
def bdecode(data, i=0):
    """返回 (对象, 下一个下标)。bytes 保持 bytes，dict 的键是 bytes。"""
    ch = data[i:i + 1]
    if ch == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = bdecode(data, i)
            v, i = bdecode(data, i)
            out[k] = v
        return out, i + 1
    if ch == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = bdecode(data, i)
            out.append(v)
        return out, i + 1
    if ch == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def bencode(v):
    if isinstance(v, int):
        return b"i" + str(v).encode() + b"e"
    if isinstance(v, bytes):
        return str(len(v)).encode() + b":" + v
    if isinstance(v, str):
        b = v.encode()
        return str(len(b)).encode() + b":" + b
    if isinstance(v, (list, tuple)):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        out = b"d"
        for k in sorted(v.keys()):
            out += bencode(k) + bencode(v[k])
        return out + b"e"
    raise TypeError("无法 bencode: %r" % type(v))


def torrent_info(raw):
    """解析 .torrent，返回 dict(hash, name, announce, size, files, wanted_meta)。"""
    met, _ = bdecode(raw, 0)
    if not isinstance(met, dict) or b"info" not in met:
        raise ValueError("不是合法 .torrent（缺 info 段）")
    info = met[b"info"]
    h = hashlib.sha1(bencode(info)).hexdigest()
    name = info.get(b"name", b"").decode("utf-8", "replace")
    if b"files" in info:                                  # 多文件
        files = [f.get(b"path", [b""])[-1].decode("utf-8", "replace")
                 for f in info[b"files"]]
        size = sum(f.get(b"length", 0) for f in info[b"files"])
    else:                                                 # 单文件
        files = [name]
        size = info.get(b"length", 0)
    anns = []
    if met.get(b"announce"):
        anns.append(met[b"announce"].decode("utf-8", "replace"))
    for grp in met.get(b"announce-list", []) or []:
        for u in grp:
            s = u.decode("utf-8", "replace")
            if s not in anns:
                anns.append(s)
    return {"hash": h, "name": name, "announces": anns, "size": size, "files": files}


def rewrite_announce(raw, new_url):
    """应急用：把 .torrent 里的 announce/announce-list 换成 new_url（info 段不动）。
    只在 rollback 里用 —— 让备份的种子能重新认盘做种，别拿它冒充站点的正式种子。"""
    met, _ = bdecode(raw, 0)
    met[b"announce"] = new_url.encode()
    met[b"announce-list"] = [[new_url.encode()]]
    met.pop(b"nodes", None)
    return bencode(met)


# ------------------------------------------------------------ qBittorrent
class _Hdrs(dict):
    """大小写不敏感的响应头。"""

    def get(self, k, d=None):
        if dict.__contains__(self, k):
            return dict.__getitem__(self, k)
        lk = k.lower()
        for kk, vv in self.items():
            if kk.lower() == lk:
                return vv
        return d


class _Resp(object):
    def __init__(self, status, body, headers):
        self.status_code = status
        self.content = body or b""
        self.headers = _Hdrs(headers or [])

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class _StdClient(object):
    """只用标准库的 HTTP 客户端，接口模仿 requests（.get/.post/.auth/.headers）。
    支持 params / data(表单) / json / files(multipart) / Basic 认证 / cookie / 302。"""

    def __init__(self, timeout=60, insecure=False):
        self.timeout = timeout
        self.auth = None
        self.headers = {}
        self.cookies = {}
        self.insecure = bool(insecure)              # https 且是自签证书时才用
        self._pool = {}                               # (scheme,host,port) -> 复用连接；Windows 频繁开关连接会把本地端口耗进 TIME_WAIT（WinError 10048）

    def _conn(self, scheme, host, port, timeout=None):
        """按协议建连接：https 默认校验证书，insecure=True 才跳过。"""
        to = timeout or self.timeout
        if scheme != "https":
            return http.client.HTTPConnection(host, port, timeout=to)
        kw = {}
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kw["context"] = ctx
        return http.client.HTTPSConnection(host, port, timeout=to, **kw)

    def _cookies(self):
        return "; ".join("%s=%s" % kv for kv in self.cookies.items())

    def _do(self, method, scheme, host, port, path, hd, body, timeout):
        """发一次 HTTP 请求：优先复用池里的 keep-alive 连接；连接被对端
        掐掉或本地端口被 TIME_WAIT 占用（WinError 10048）时自动重连并退避重试。"""
        key = (scheme, host, port)
        last = None
        for attempt in range(5):
            conn = self._pool.get(key)
            if conn is None:
                conn = self._conn(scheme, host, port, timeout)
                self._pool[key] = conn
            try:
                conn.request(method, path, body=body, headers=hd)
                resp = conn.getresponse()
                raw = resp.read()
                pairs = list(resp.getheaders())
                if getattr(resp, "will_close", False):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._pool.pop(key, None)
                return resp.status, raw, pairs
            except Exception as e:
                last = e
                try:
                    conn.close()
                except Exception:
                    pass
                self._pool.pop(key, None)
                time.sleep(1.5 * (attempt + 1))
        raise last

    def _keep(self, hdr_pairs):
        for k, v in hdr_pairs:
            if k.lower() == "set-cookie":
                name, _, val = v.split(";", 1)[0].partition("=")
                self.cookies[name.strip()] = val.strip()

    def request(self, method, url, params=None, data=None, json_body=None,
                files=None, headers=None, timeout=None):
        u = urlparse(url)
        scheme = (u.scheme or "http").lower()
        host, _, port_s = (u.netloc or "").partition(":")
        port = int(port_s) if port_s.isdigit() else (443 if scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        if params:
            enc = urlencode([(k, "" if v is None else str(v)) for k, v in params.items()])
            path += ("&" if "?" in path else "?") + enc
        hd = {"Accept": "*/*", "User-Agent": "tracker-replace/1.0"}
        hd.update(self.headers)
        if headers:
            hd.update(headers)
        if self.cookies:
            hd["Cookie"] = self._cookies()
        if self.auth:
            hd["Authorization"] = "Basic " + base64.b64encode(
                ("%s:%s" % self.auth).encode("utf-8")).decode()
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hd["Content-Type"] = "application/json"
        elif files is not None:
            bnd = "----trackerreplace" + binascii.hexlify(os.urandom(8)).decode()
            parts = []
            for k, v in (data or {}).items():
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                              % (bnd, k, "" if v is None else str(v))).encode("utf-8"))
            for k, spec in files.items():
                fname, content, ctype = spec
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                              "Content-Type: %s\r\n\r\n" % (bnd, k, fname, ctype)).encode("utf-8"))
                parts.append((content if isinstance(content, bytes) else str(content).encode()) + b"\r\n")
            parts.append(("--%s--\r\n" % bnd).encode("utf-8"))
            body = b"".join(parts)
            hd["Content-Type"] = "multipart/form-data; boundary=" + bnd
        elif data is not None:
            body = urlencode([(k, "" if v is None else str(v)) for k, v in data.items()]).encode()
            hd["Content-Type"] = "application/x-www-form-urlencoded"
        if body is not None:
            hd["Content-Length"] = str(len(body))
        last = None
        for _ in range(6):                            # 处理 301/302/307 跳转
            code, raw, pairs = self._do(method, scheme, host, port, path, hd, body, timeout)
            self._keep(pairs)
            last = _Resp(code, raw, pairs)
            if code in (301, 302, 303, 307, 308):
                loc = last.headers.get("Location") or ""
                if not loc:
                    return last
                nu = urlparse(loc if "://" in loc else "%s://%s:%d%s" % (scheme, host, port, loc))
                scheme, host = (nu.scheme or scheme).lower(), nu.hostname or host
                port = nu.port or (443 if scheme == "https" else 80)
                path = nu.path or "/"
                if nu.query:
                    path += "?" + nu.query
                method, body = "GET", None
                hd.pop("Content-Type", None)
                hd.pop("Content-Length", None)
                continue
            return last
        return last

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params, timeout=timeout)

    def post(self, url, data=None, json_body=None, files=None, params=None, timeout=None):
        return self.request("POST", url, params=params, data=data,
                            json_body=json_body, files=files, timeout=timeout)


class QbClient:
    kind = "qb"

    def __init__(self, host, port, user="", pw="", scheme="http", timeout=60, insecure=False):
        self.scheme = (scheme or "http").lower()
        self.base = "%s://%s:%d" % (self.scheme, host, port)
        self.timeout = timeout
        self.insecure = bool(insecure)
        self.s = _StdClient(timeout=timeout, insecure=insecure)
        r = self.s.post(self.base + "/api/v2/auth/login",
                        data={"username": user, "password": pw}, timeout=timeout)
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise RuntimeError("qB 登录失败 HTTP %s（若 403 请检查 WebUI 白名单/CSRF）"
                               % r.status_code)
        self.api = self._get("/api/v2/app/webapiVersion")
        self.version = self._get("/api/v2/app/version")
        # skip_checking 自 qB 3.3.13(2017) 起就有；v5.3.0 已知会忽略该参数
        try:
            self.vtuple = tuple(int(x) for x in self.version.lstrip("v").split(".")[:3])
        except ValueError:
            self.vtuple = (0, 0, 0)

    def _get(self, path, **params):
        r = self.s.get(self.base + path, params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError("%s → HTTP %s" % (path, r.status_code))
        t = r.text.strip()
        try:
            return json.loads(t)
        except ValueError:
            return t

    # ---- 能力 ----
    def caps(self):
        skip = True
        note = "支持 skip_checking（免哈希校验）"
        if self.vtuple >= (5, 3, 0) and self.vtuple < (5, 3, 1):
            note = "⚠ 5.3.0 已知 bug：skip_checking 可能被忽略，加入后会走校验"
            skip = False
        return {"skip_check": skip, "note": note,
                "desc": "qBittorrent %s (WebAPI %s)" % (self.version, self.api)}

    # ---- 读 ----
    def torrents(self):
        return self._get("/api/v2/torrents/info", filter="all")

    def trackers(self, h):
        try:
            j = self._get("/api/v2/torrents/trackers", hash=h)
            return [x.get("url") or "" for x in j
                    if x.get("url") and not x.get("url").startswith("**")]
        except Exception:
            return []

    def list_trackers(self, ts):
        """批量取 tracker（qB 只能逐种取，线程池加速）"""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            return list(ex.map(lambda t: self.trackers(t["hash"]), ts))

    def detail(self, h):
        p = self._get("/api/v2/torrents/properties", hash=h)
        fl = self._get("/api/v2/torrents/files", hash=h)
        return p, fl

    def dump(self, h):
        """导出该种子的 .torrent 原始字节（qB 独有，TR 没有对应能力）"""
        r = self.s.get(self.base + "/api/v2/torrents/exportFile",
                       params={"hash": h}, timeout=self.timeout)
        return r.content if r.status_code == 200 and len(r.content) > 60 else None

    def state(self, h):
        try:
            info = self._get("/api/v2/torrents/info", filter="all")
        except Exception:
            return None
        for t in info:
            if t["hash"] == h or t.get("infohash_v1") == h:
                return {"id": t["hash"], "pct": round((t.get("progress") or 0) * 100, 2),
                        "status": t.get("state"), "downloaded": t.get("downloaded") or 0,
                        "trackers": self.trackers(t["hash"]),
                        "done": t.get("state") in QB_UP and (t.get("progress") or 0) >= 0.999,
                        "checking": "checking" in (t.get("state") or ""),
                        "downloading": t.get("state") in QB_DL}
        return None

    # ---- 写 ----
    def add(self, raw, save_path, paused=False, new_url=None, category="", tags="",
            skip_check=True, limits=None, layout=None):
        data = {"savepath": save_path, "category": category or "", "tags": tags or "",
                "skip_checking": "true" if skip_check else "false",
                "paused": "true" if paused else "false",
                "contentLayout": layout or "Original"}
        if new_url:
            data["trackers"] = new_url
        for k, key in (("up_limit", "uplimit"), ("down_limit", "dllimit"),
                       ("ratio_limit", "ratio_limit"), ("seeding_time_limit", "seeding_time_limit")):
            if limits and limits.get(k) not in (None, ""):
                data[key] = limits[k]
        files = {"torrents": ("t.torrent", raw, "application/x-bittorrent")}
        r = self.s.post(self.base + "/api/v2/torrents/add", files=files, data=data,
                        timeout=self.timeout * 2)
        body = (r.text or "").strip()
        # 成败看正文（源码 TorrentsController::addTorrents）：加进来至少一个 → "Ok."，
        # 一个都没加进来 → "Fails."（重复 hash 的私有种子正是这种）；老版本正文为空。
        if r.status_code != 200 or (body and not body.startswith("Ok")):
            raise RuntimeError("qb /torrents/add 没成功 HTTP %s %s"
                               "（Fails. 常见原因：该 hash 还被 qB 占用（删条目没删完）"
                               "或种子文件无效）"
                               % (r.status_code, body[:160] or "空响应"))

    def find(self, ih):
        for t in self.torrents():
            if t.get("infohash_v1", t["hash"]).lower() == ih.lower():
                return t["hash"]
        return None

    def set_file_prio(self, h, prio_map):
        """prio_map: {文件id: 0|1|2}，用 name→id 映射后调用"""
        if not prio_map:
            return
        ids = ",".join(str(i) for i in prio_map)
        self.s.post(self.base + "/api/v2/torrents/filePrio",
                    data={"hash": h, "id": ids, "priority": "0"}, timeout=self.timeout)

    def remove(self, h):
        self.s.post(self.base + "/api/v2/torrents/delete",
                    data={"hashes": json.dumps([h]), "deleteFiles": "false"},
                    timeout=self.timeout)

    def start(self, h):
        self.s.post(self.base + "/api/v2/torrents/resume",
                    data={"hashes": json.dumps([h])}, timeout=self.timeout)

    def stop(self, h):
        self.s.post(self.base + "/api/v2/torrents/pause",
                    data={"hashes": json.dumps([h])}, timeout=self.timeout)

# ------------------------------------------------------------ Transmission
class TrClient:
    kind = "tr"

    def __init__(self, host, port, user="", pw="", scheme="http", timeout=120, insecure=False):
        self.scheme = (scheme or "http").lower()
        self.url = "%s://%s:%d/transmission/rpc" % (self.scheme, host, port)
        self.timeout = timeout
        self.insecure = bool(insecure)
        self.s = _StdClient(timeout=timeout, insecure=insecure)
        if user:
            self.s.auth = (user, pw)
        self.s.headers["X-Transmission-Session-Id"] = "0"
        r = self._raw("session-get", timeout=min(timeout, 90))
        a = r.get("arguments", {})
        sess = a.get("session") or a                    # TR4 在 arguments.session，老版平铺
        self.version = sess.get("version", "?")
        self.rpcver = sess.get("rpc-version", "?")

    def _raw(self, method, arguments=None, timeout=None):
        body = {"method": method}
        if arguments:
            body["arguments"] = arguments
        for _ in range(3):
            r = self.s.post(self.url, json_body=body, timeout=timeout or self.timeout)
            if r.status_code == 409:                    # CSRF 头，必须取回来重发
                self.s.headers["X-Transmission-Session-Id"] = \
                r.headers.get("X-Transmission-Session-Id", "")
                continue
            if r.status_code == 401:
                raise RuntimeError("TR 认证失败(401)")
            if r.status_code != 200:
                raise RuntimeError("TR HTTP %s" % r.status_code)
            j = r.json()
            if j.get("result") not in ("success", None, ""):
                raise RuntimeError("%s: %s" % (method, j.get("result")))
            return j
        raise RuntimeError("拿不到 X-Transmission-Session-Id")

    def call(self, method, arguments=None, timeout=None):
        return self._raw(method, arguments, timeout).get("arguments", {})

    # ---- 能力 ----
    def caps(self):
        return {"skip_check": False,
                "note": "RPC 无跳过校验参数（所有版本），只能重校验；工具会守住零回流",
                "desc": "Transmission %s (RPC v%s)" % (self.version, self.rpcver)}

    LIST_FIELDS = ["id", "name", "hashString", "status", "percentDone", "error",
                   "errorString", "downloadedEver", "trackers", "isPrivate",
                   "totalSize", "downloadDir", "labels", "addedDate"]

    def torrents(self, fields=None):
        a = self.call("torrent-get", {"fields": fields or self.LIST_FIELDS}, timeout=900)
        return a.get("torrents", [])

    def detail(self, tid):
        a = self.call("torrent-get", {"ids": [tid], "fields":
                      ["id", "name", "downloadDir", "files", "fileStats", "labels",
                       "ratioLimit", "ratioMode", "seedIdleLimit", "seedIdleMode",
                       "uploadLimit", "downloadLimit", "peerLimit", "trackers"]})
        return a["torrents"][0] if a.get("torrents") else None

    def state(self, tid):
        try:
            a = self.call("torrent-get", {"ids": [tid], "fields":
                          ["id", "percentDone", "recheckProgress", "status", "error",
                           "errorString", "downloadedEver", "leftUntilDone", "trackers"]})
        except Exception:
            return None
        if not a.get("torrents"):
            return None
        t = a["torrents"][0]
        st = t.get("status")
        return {"id": t["id"], "pct": round((t.get("percentDone") or 0) * 100, 2),
                "status": st, "recheck": round((t.get("recheckProgress") or 0) * 100, 1),
                "error": t.get("error"), "errorString": t.get("errorString", ""),
                "downloaded": int(t.get("downloadedEver") or 0),
                "trackers": [x.get("announce") or "" for x in t.get("trackers", [])],
                "done": st == TR_SEED and (t.get("percentDone") or 0) >= 0.999,
                # TR 对新加入种子的首次校验也报 status=4，只有真的下过东西才算回流
                "downloading": st == TR_DL and (t.get("downloadedEver") or 0) > 0,
                "checking": st in TR_CHECKING or (st == TR_DL and (t.get("recheckProgress") or 0) > 0)}

    def find(self, ih):
        for t in self.call("torrent-get", {"fields": ["id", "hashString"]},
                           timeout=900).get("torrents", []):
            if (t.get("hashString") or "").lower() == ih.lower():
                return t["id"]
        return None

    def add(self, raw, download_dir, paused=True, wanted=None, unwanted=None,
            labels=None, new_url=None):
        args = {"metainfo": base64.b64encode(raw).decode(),
                "download-dir": download_dir, "paused": bool(paused)}
        if unwanted:
            args["files_unwanted"] = list(unwanted)
        elif wanted:
            args["files_wanted"] = list(wanted)
        if labels:
            args["labels"] = list(labels)
        if new_url:
            args["trackerAdd"] = [new_url]
        a = self.call("torrent-add", args, timeout=300)
        obj = a.get("torrent-added") or a.get("torrent")
        if obj:
            return obj["id"]
        dup = a.get("torrent-duplicate")
        if isinstance(dup, dict) and dup.get("id") is not None:
            want = ""
            try:
                want = torrent_info(raw)["hash"]
            except Exception:
                pass
            if want and (dup.get("hashString") or "").lower() == want:
                return dup["id"]      # 同 hash 的条目本来就在：当作已加入（回滚幂等）
            raise RuntimeError("TR 拒加：客户端已有一个同名但不同 hash 的种子 —— %s（%s），"
                               "先把它删掉或把数据挪个目录再操作"
                               % (dup.get("name", "?"), (dup.get("hashString") or "?")[:12]))
        raise RuntimeError("TR 未返回 torrent-added: %s"
                           % json.dumps(a, ensure_ascii=False)[:120])

    def remove(self, tid):
        self.call("torrent-remove", {"ids": [tid], "delete-local-data": False})

    def start(self, tid):
        self.call("torrent-start", {"ids": [tid]})

    def stop(self, tid):
        self.call("torrent-stop", {"ids": [tid]})

    def dl_cap(self, tid, kbps):
        """把该种子的下载限速设成 kbps（KB/s，0=不限）。校验期用它杜绝下载。"""
        self.call("torrent-set", {"ids": [tid], "downloadLimited": bool(kbps),
                                  "downloadLimit": int(kbps)})

    def set_limits(self, tid, d):
        args = {"ids": [tid]}
        for k in ("ratioLimit", "ratioMode", "seedIdleLimit", "seedIdleMode",
                  "uploadLimit", "downloadLimit", "peerLimit"):
            if d.get(k) is not None:
                args[k] = d[k]
        if len(args) > 1:
            try:
                self.call("torrent-set", args)
            except Exception:
                pass


CLIENTS = {"qb": QbClient, "tr": TrClient}


def connect(inst):
    """自动识别：先当 qB 试，不行再当 TR 试。"""
    kind = inst.get("kind")
    order = [kind] if kind in CLIENTS else ["qb", "tr"]
    last = None
    for k in order:
        try:
            return CLIENTS[k](inst["host"], inst["port"], inst.get("user", ""),
                              inst.get("password") or os.environ.get("TRREPLACE_PASSWORD", ""),
                              (inst.get("scheme") or "http").lower(),
                              insecure=bool(inst.get("insecure")))
        except Exception as e:
            last = e
    raise RuntimeError("识别失败（qB/TR 都连不上）：%s" % str(last)[:90])


# --------------------------------------------------------------- 台账/结果
def load_ledger():
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            log("⚠ 台账损坏，重新计数")
    return {}


def save_ledger(L):
    os.makedirs(ST_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(L, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LEDGER)


def result_row(rec):
    new = not os.path.exists(RESULTS)
    with open(RESULTS, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["时间", "实例", "结果", "名称", "大小GB", "hash",
                        "旧id", "新id", "tracker已换", "跳过校验", "原因"])
        w.writerow([rec.get("ts"), rec.get("inst"), rec.get("verdict"), rec.get("name"),
                    round((rec.get("size") or 0) / 2 ** 30, 2), rec.get("hash"),
                    rec.get("old_id"), rec.get("new_id"), rec.get("tracker_ok"),
                    rec.get("skip_check"), rec.get("why")])


# ------------------------------------------------------------ 新种子来源
def load_torrent_index(a):
    """把新种子文件按 info_hash 建索引。
    --torrents-dir：递归扫目录里所有 .torrent（按 info hash 配对，与文件名无关，最稳）
    --csv        ：每行 `torrent=<.torrent 路径>`，可选 `hash=`（站点 info 段也变了时用）
    """
    idx = {}
    files = []
    if a.torrents_dir:
        for root, _d, fs in os.walk(a.torrents_dir):
            for fn in fs:
                if fn.lower().endswith(".torrent"):
                    files.append(os.path.join(root, fn))
    csv_hash = {}                                        # path -> 指定 hash
    if a.csv:
        with open(a.csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                p = (row.get("torrent") or row.get("path") or "").strip()
                if not p or not os.path.exists(p):
                    continue
                files.append(p)
                h = (row.get("hash") or row.get("old_hash") or "").strip().lower()
                if h:
                    csv_hash[os.path.abspath(p)] = h
    for p in files:
        try:
            raw = open(p, "rb").read()
            m = torrent_info(raw)
        except Exception as e:
            log("  跳过打不开的种子 %s（%s）" % (os.path.basename(p), str(e)[:40]))
            continue
        e = {"path": p, "raw": raw, "meta": m}
        idx.setdefault(m["hash"], e)
        ap = os.path.abspath(p)
        if ap in csv_hash:
            idx.setdefault(csv_hash[ap], e)
    return idx


# ------------------------------------------------------------------ 处理
def pick_targets(client, a, L, inst_key):
    """挑出要处理的种子：tracker 命中 --old、不在台账已完成、可选 --only/--limit"""
    ts = client.torrents()
    if client.kind == "qb":
        trks = client.list_trackers(ts)
        rows = []
        for t, trk in zip(ts, trks):
            rows.append({"id": t["hash"], "hash": t.get("infohash_v1", t["hash"]).lower(),
                         "name": t.get("name"), "size": t.get("size") or 0,
                         "pct": (t.get("progress") or 0) * 100, "status": t.get("state"),
                         "path": t.get("save_path"), "trackers": trk,
                         "category": t.get("category"), "tags": t.get("tags"),
                         "done": t.get("state") in QB_UP and (t.get("progress") or 0) >= 0.999})
    else:
        rows = []
        for t in ts:
            rows.append({"id": t["id"], "hash": (t.get("hashString") or "").lower(),
                         "name": t.get("name"), "size": t.get("totalSize") or 0,
                         "pct": (t.get("percentDone") or 0) * 100, "status": t.get("status"),
                         "path": t.get("downloadDir"),
                         "trackers": [x.get("announce") or "" for x in t.get("trackers", [])],
                         "labels": t.get("labels") or [],
                         "done": t.get("status") == TR_SEED and (t.get("percentDone") or 0) >= 0.999})
    out = []
    for r in rows:
        hit = [u for u in r["trackers"] if a.old in u]
        if not hit:
            continue
        r["old_url"] = hit[0]
        if r["hash"] in L.get(inst_key, {}).get("done", {}):
            continue
        if a.only and a.only not in (r["name"] or "") and a.only.lower() not in r["hash"]:
            continue
        out.append(r)
        if a.limit and len(out) >= a.limit:
            break
    return out


def new_torrent_for(a, idx, r):
    """新种子只能来自站点下载的那份 .torrent。

    不做"原地改 tracker"，也不做"拿旧种子改 announce"—— 站点给的新种子是另一份
    文件（announce-list / created by / comment 都可能不同，甚至 info 段也重做过），
    改出来的那份 tracker 不认。所以只有一条路：
        加新种子 → 确认做种且零回流 → 再删旧种子。
    配对方式：info hash 完全相等（与文件名无关，站点改文件名不影响）。
    """
    e = idx.get(r["hash"])
    if not e:
        same = [v for v in idx.values() if v["meta"]["name"] == r["name"]]
        hint = ""
        if same:
            hint = ("；目录里有同名种子 %s 但 info hash 不同 —— 站点重做过种子，"
                    "hash 对不上就不敢自动换（换了等于重新下一遍数据）"
                    % os.path.basename(same[0]["path"]))
        raise RuntimeError("按 info hash 配不到新种子文件%s。请把站点下载的新种子全量放进 "
                           "--torrents-dir（或 --csv 指定路径）" % hint)
    anns = e["meta"]["announces"]
    return e["raw"], (anns[0] if anns else None), "按 hash 配到 %s" % os.path.basename(e["path"])


def _qb_tracker_ok(fin, new_url, a):
    urls = (fin or {}).get("trackers") or []
    want = new_url or a.new_url
    if want:
        return any(want in (u or "") for u in urls)
    return not any(a.old in (u or "") for u in urls)


def _qb_wait_gone(client, ih, tries=30):
    """等旧条目真的从会话里消失（qB 删条目是异步的，马上加新的会被吞掉）"""
    for _ in range(tries):
        if not client.find(ih):
            return True
        time.sleep(0.5)
    return False


def _qb_add_retry(client, raw, save_path, tries=5, gap=2.0, **kw):
    """qB ≥ 4.6 删条目是异步的：列表里条目消失了，SessionImpl 里对这个 hash 的
    占用未必已释放，这时 /torrents/add 回 HTTP 200 + "Fails."，等几秒重试即可成功
    ——实测有人在这里看到 200 Fails. 就以为坏了，其实是删除滞后的瞬时状态。
    所以 add 失败先重试几轮，而不是第一枪就判死。"""
    last = None
    n = max(1, int(tries))
    for i in range(n):
        try:
            client.add(raw, save_path, **kw)
            return i + 1
        except RuntimeError as e:
            last = str(e)
            if i + 1 < n and gap:
                time.sleep(gap)
    raise RuntimeError("%s（重试 %d 次仍被 qB 拒绝）" % ((last or "add 失败")[:140], n))


def _qb_wait_id(client, ih, tries=30):
    for _ in range(tries):
        nid = client.find(ih)
        if nid:
            return nid
        time.sleep(1)
    return None


def _qb_restore_old(client, bk, r, save_path, unwanted=None):
    """把旧条目原样加回并继续做种 —— 数据从头到尾没动过，这一步总是安全的。
    注意：若该 hash 其实还挂在列表里（旧条目只是赖着没删掉），本来就没什么可回灌的，
    再 add 一次只会再吃一发 Fails.；确认它还活着并让它继续做种即可。"""
    st = {}
    try:
        st = json.load(open(bk["state"], encoding="utf-8"))
    except Exception:
        pass
    still = client.find(r["hash"])
    if still:                                   # hash 还占着：条目没丢，确认在种就走
        try:
            fin = client.state(still)
            if fin and not fin["checking"]:
                client.start(still)
            return True
        except Exception:
            return True
    p = (bk or {}).get("old_torrent") or (bk or {}).get("new_torrent")
    if not p or not os.path.exists(p):
        return False
    raw = open(p, "rb").read()
    if st.get("old_url"):
        try:
            raw = rewrite_announce(raw, st["old_url"])
        except Exception:
            pass
    _qb_wait_gone(client, r["hash"])
    try:
        _qb_add_retry(client, raw, save_path, tries=3, gap=2.0, paused=True,
                      category=st.get("category") or r.get("category") or "",
                      tags=st.get("tags") or r.get("tags") or "", skip_check=True)
    except RuntimeError:
        return False
    nid = _qb_wait_id(client, r["hash"])
    if nid and unwanted:
        try:
            _d, nf = client.detail(nid)
            client.set_file_prio(nid, {f["id"]: 0 for f in nf if f["name"] in unwanted})
        except Exception:
            pass
    if nid:
        try:
            client.start(nid)                        # 回灌后照旧做种，别因为工具失败断种
        except Exception:
            pass
    return bool(nid)


def _qb_watch(client, nid, a, seconds=60):
    """盯状态到能下结论：做种态 / 在下载 / 超时"""
    st = None
    for _ in range(seconds):
        st = client.state(nid)
        if st and (st["done"] or st["downloading"] or st["downloaded"] > 0):
            return st
        time.sleep(1)
    return st


def do_qb(client, r, raw, new_url, a, dry, bk=None):
    """qB 路线，两种情况分开走（差别就在 info hash 变没变）：

    A 新旧 hash 不同（站点重做过 info 段）：加新(skip_checking，暂停态) → 确认做种态
      + tracker 对 + 零回流 → 才删旧。旧种子在确认前绝不动，最安全。

    B 新旧 hash 相同（站点只换了 tracker，实际最常见）：qB 一个 hash 只能存在一份 ——
      源码 SessionImpl::addTorrent_impl 里私有种子碰到重复 hash 直接 return false，
      WebAPI 回 "Fails."，所以 A 这条路走不通。只能：删条目(数据留盘) → 确认条目消失
      → 加新(暂停 + skip_checking) → 确认通过才启动做种；中途任何一步不对，就用备份把
      旧条目原样加回继续做种，绝不留下"两头空"。
    """
    detail, files = client.detail(r["id"])
    save_path = detail.get("save_path") or r["path"]
    if not save_path:
        raise RuntimeError("save_path 取不到，拒动")
    if not raw:
        raise RuntimeError("没有可用 .torrent")
    new_hash = torrent_info(raw)["hash"]
    same = (new_hash == r["hash"])
    unwanted = [f["name"] for f in files if (f.get("priority") or 0) == 0]
    limits = {k: detail.get(k) for k in ("up_limit", "down_limit", "ratio_limit",
                                         "seeding_time_limit")}
    if dry:
        return "演练", {"would": ("同 hash：删条目(留数据) → 加新 → 确认 → 不对就自动回灌"
                                  if same else
                                  "加新(skip_checking) → 确认 → 才删旧"),
                        "path": save_path, "unwanted": len(unwanted), "same_hash": same}

    if same:                                        # --- B：同 hash，只能先让位 ---
        client.remove(r["id"])
        wl = max(2, int(getattr(a, "wait_limit", 30)))
        if not _qb_wait_gone(client, r["hash"], tries=wl * 2):
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("旧条目删了还赖在列表里（qB 还在卸载），已%s，未继续"
                               % ("回灌" if ok else "尝试回灌失败，请手动检查"))
        try:
            _qb_add_retry(client, raw, save_path,
                          tries=max(2, int(getattr(a, "add_tries", 5))),
                          gap=float(getattr(a, "add_gap", 2.0)),
                          paused=True, new_url=new_url,
                          category=r.get("category") or "", tags=r.get("tags") or "",
                          skip_check=True, limits=limits, layout=a.layout)
        except RuntimeError as e:
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("新种子没能加进去（%s），旧条目已%s"
                               % (str(e)[:110], "回灌、继续做种" if ok
                                  else "尝试回灌但没成功，请手动加回（数据在盘上没动）"))
        nid = _qb_wait_id(client, r["hash"], tries=max(2, int(getattr(a, "wait_limit", 30))))
        if not nid:
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("新种子没能加进去（qB 回绝），已%s旧条目"
                               % ("回灌" if ok else "回灌失败，请手动加回"))
        st = _qb_watch(client, nid, a)
        if st and (st["downloaded"] > 0 or (st["downloading"] and not st["checking"])):
            try:
                client.stop(nid)
            except Exception:
                pass
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("加入后它开始下载（回流 %d 字节）= skip_checking 没生效，"
                               "已撤销新种子并%s旧条目" % (st["downloaded"], "回灌" if ok else "回灌失败"))
        if not (st and st["done"]):
            try:
                client.stop(nid)
            except Exception:
                pass
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("没做到 100%%（pct=%s state=%s），已停住并%s旧条目，没让它下载"
                               % (st and st["pct"], st and st["status"],
                                  "回灌" if ok else "回灌失败"))
        fin = client.state(nid) or st
        if not _qb_tracker_ok(fin, new_url, a):
            try:
                client.stop(nid)
            except Exception:
                pass
            ok = _qb_restore_old(client, bk, r, save_path, unwanted)
            raise RuntimeError("新种子的 tracker 不是期望的新地址，已停住并%s旧条目"
                               % ("回灌" if ok else "回灌失败"))
        if unwanted:
            try:
                _d, nf = client.detail(nid)
                client.set_file_prio(nid, {f["id"]: 1 for f in nf if f["name"] in unwanted})
            except Exception as e:
                log("    （恢复文件勾选失败：%s）" % str(e)[:40])
        if not a.pause:
            try:
                client.start(nid)
            except Exception as e:
                log("    （启动做种失败：%s）" % str(e)[:60])
        else:
            try:
                client.stop(nid)
            except Exception:
                pass
        return "ok", {"new_id": nid, "pct": fin["pct"], "state": fin["status"],
                      "downloaded": fin["downloaded"], "tracker_ok": _qb_tracker_ok(fin, new_url, a),
                      "path": save_path, "skip_check": True, "same_hash": True}

    # --- A：hash 不同，先加新，确认通过才动旧种子 ---
    if client.find(new_hash):
        return "hashbusy", {"why": "qB 里已存在同 hash 种子（此前已换过或输入里重复），"
                                   "旧条目未动", "path": save_path, "same_hash": False}
    try:
        _qb_add_retry(client, raw, save_path,
                      tries=max(2, int(getattr(a, "add_tries", 5))),
                      gap=float(getattr(a, "add_gap", 2.0)),
                      paused=True, new_url=new_url,
                      category=r.get("category") or "", tags=r.get("tags") or "",
                      skip_check=True, limits=limits, layout=a.layout)
    except RuntimeError as e:
        raise RuntimeError("新种子没能加进去（%s），旧条目未动，数据无损" % str(e)[:110])
    nid = None
    for _ in range(20):
        nid = client.find(new_hash)            # 找新 hash！这里曾经踩过坑：用旧 hash 找
        if nid:                                # 会把还没删的旧种子当成新的，判断全错
            break
        time.sleep(1)
    if not nid:
        raise RuntimeError("新种子没出现（旧种子未删，数据无损）")
    st = _qb_watch(client, nid, a, 40)
    if not st:
        raise RuntimeError("新种子读不到状态（旧种子未删）")
    if st["downloaded"] > 0 or (st["downloading"] and not st["checking"]):
        client.remove(nid)
        raise RuntimeError("回流 %d 字节 → 已撤销新种子；说明 skip_checking 没生效"
                           "（qB 5.3.0 有这问题），旧种子未删" % st["downloaded"])
    fin = None
    for _ in range(30):
        fin = client.state(nid)
        if fin and fin["done"]:
            break
        time.sleep(2)
    if not (fin and fin["done"]):
        raise RuntimeError("新种子未进入做种态（pct=%s state=%s），旧种子未删"
                           % (fin and fin["pct"], fin and fin["status"]))
    if not _qb_tracker_ok(fin, new_url, a):
        raise RuntimeError("新种子 tracker 不是期望的新地址，旧种子未删，先别做种")
    if unwanted:                                          # 恢复"不下载"的文件勾选
        try:
            _d, nfiles = client.detail(nid)
            prio = {f["id"]: 1 for f in nfiles if f["name"] in unwanted}
            client.set_file_prio(nid, prio)
        except Exception as e:
            log("    （恢复文件勾选失败：%s）" % str(e)[:40])
    if not a.pause:                              # 确认通过（做种态+tracker对+零回流）才启动
        try:
            client.start(nid)
        except Exception as e:
            log("    （启动做种失败：%s）" % str(e)[:60])
    else:
        try:
            client.stop(nid)
        except Exception:
            pass
    client.remove(r["id"])
    return "ok", {"new_id": nid, "pct": fin["pct"], "state": fin["status"],
                  "downloaded": fin["downloaded"], "tracker_ok": True,
                  "path": save_path, "skip_check": True, "same_hash": False}


def do_tr(client, r, raw, new_url, a, dry, bk=None):
    """TR 路线：被迫先删后加（同 hash 不能并存 + 私有种子禁止改 tracker）。
       所以：备份/快照必须已成功，且加完立刻盯校验，一回流就停+回滚。"""
    d = client.detail(r["id"])
    if not d:
        raise RuntimeError("读不到旧种子详情")
    dd = d.get("downloadDir") or r["path"]
    if not dd:
        raise RuntimeError("downloadDir 为空，拒动")
    files = d.get("files") or []
    stats = d.get("fileStats") or []
    if not files:
        raise RuntimeError("文件列表为空，拒动")
    unwanted = [i for i, s in enumerate(stats) if s is not None and not s.get("wanted", True)]
    labels = d.get("labels") or r.get("labels") or []
    if dry:
        return "演练", {"would": "remove(保留数据) → add(paused) → start → 盯校验",
                        "path": dd, "files": len(files), "unwanted": len(unwanted)}
    # 1) 删旧（保留盘上数据）
    client.remove(r["id"])
    # 2) 暂停态加入，顺带把"不下载"的文件保持不下载
    try:
        nid = client.add(raw, dd, paused=True, unwanted=unwanted or None,
                         labels=labels or None, new_url=None if new_url else a.new_url)
    except Exception as e:
        raise RuntimeError("加新种子失败（旧种子已删，可用 rollback 子命令还原）：%s" % str(e)[:110])
    # 3) 校验期间把该种子下载限速压到 1KB/s：读盘算哈希不吃带宽，
    #    这样即使盘上有缺口，它也几乎下不动东西 —— 物理上保证"不过就不下载"
    capped = False
    if not getattr(a, "no_cap", False):
        try:
            client.dl_cap(nid, 1)
            capped = True
        except Exception as e:
            log("    （校验限速没加上：%s）" % str(e)[:60])
    # 4) 启动 → TR 才开始读盘校验
    try:
        client.start(nid)
    except Exception:
        pass
    verdict, st = "verify?", None
    t0 = time.time()
    while time.time() - t0 < a.check_budget:
        st = client.state(nid)
        if not st:
            break
        if st["downloaded"] > 0:                     # 真下东西了 = 盘上数据不完整
            client.stop(nid)
            verdict = "backflow"
            break
        if st["done"]:                               # status=做种中 且 100% 才算通过
            verdict = "checked"
            break
        if st["recheck"] >= 100.0 and st["pct"] < 99.999:
            client.stop(nid)                         # 校验跑完但盘上有缺口 → 立刻停
            verdict = "incomplete"
            break
        time.sleep(a.poll)
    else:
        verdict = "timeout"
    fin = client.state(nid) or st or {}
    if capped:                                       # 解除限速（要不要做种看结论）
        try:
            client.dl_cap(nid, 0)
        except Exception:
            pass
    trk = fin.get("trackers") or []
    want = new_url or a.new_url
    trk_ok = any((want or "") in (u or "") for u in trk) if want else \
        not any(a.old in (u or "") for u in trk)
    if verdict == "incomplete":
        client.stop(nid)
        return "incomplete", {"new_id": nid, "pct": fin.get("pct"), "status": fin.get("status"),
                              "downloaded": fin.get("downloaded"), "tracker_ok": trk_ok,
                              "path": dd,
                              "why": "校验未通过（盘上数据有缺口），已停住并保持暂停，不会自动下载"}
    if verdict == "backflow":
        return "backflow", {"new_id": nid, "pct": fin.get("pct"), "downloaded": fin.get("downloaded"),
                            "tracker_ok": trk_ok, "path": dd,
                            "why": "盘上数据有缺口（缺 %.1f MB），已停住没继续下"
                                   % ((fin.get("downloaded") or 0) / 2 ** 20)}
    if not (fin.get("done") and trk_ok):
        try:
            client.stop(nid)                         # 没过就不许它跑起来
        except Exception:
            pass
        return "notready", {"new_id": nid, "pct": fin.get("pct"), "status": fin.get("status"),
                            "tracker_ok": trk_ok, "path": dd,
                            "why": {"timeout": "校验 %d 秒还没跑完，已先停住（没让它下载）"
                                    % a.check_budget,
                                    "verify?": "没读到校验状态，已先停住"}.get(verdict, verdict)}
    if a.pause:                                      # 通过了，但用户要求先别做种（--pause）
        try:
            client.stop(nid)
        except Exception:
            pass
    client.set_limits(nid, d)
    return "ok", {"new_id": nid, "pct": fin.get("pct"), "status": fin.get("status"),
                  "downloaded": fin.get("downloaded", 0), "tracker_ok": trk_ok,
                  "path": dd, "skip_check": False}


# ------------------------------------------------------------------ 子命令
def cmd_doctor(a):
    insts = load_instances(a)
    print("%-26s %-36s %-12s %s" % ("目标", "客户端", "跳过校验", "tracker 命中数"))
    for inst in insts:
        k = inst["label"]
        try:
            c = connect(inst)
            n = len(pick_targets(c, a, {}, k))
            cp = c.caps()
            print("%-26s %-36s %-12s %d" % (k, cp["desc"], "支持" if cp["skip_check"] else "不支持", n))
        except Exception as e:
            print("%-26s 连不上：%s" % (k, str(e)[:60]))
    print("\n说明：TR 的「不支持」是 Transmission RPC 本身的限制（所有版本都没有"
          "跳过校验的参数），工具会走「暂停加入→启动校验→零回流守护」。")


def load_instances(a):
    if a.config:
        cfg = json.load(open(a.config, encoding="utf-8"))
        insts = cfg if isinstance(cfg, list) else cfg.get("instances", [])
    elif a.host and a.port:
        insts = [{"label": "%s:%s" % (a.host, a.port), "host": a.host, "port": a.port,
                  "kind": a.kind or "", "user": a.user or "",
                  "scheme": getattr(a, "scheme", None) or "http",
                  "insecure": bool(getattr(a, "insecure", False)),
                  "password": a.password or os.environ.get("TRREPLACE_PASSWORD", "")}]
    else:
        die("要指定实例：--config config.json 或 --host/--port/--user/--password")
    for i in insts:
        i.setdefault("label", "%s:%s" % (i.get("host"), i.get("port")))
        i.setdefault("password", os.environ.get("TRREPLACE_PASSWORD", ""))
        s = str(i.get("scheme") or "http").lower().rstrip(":/")
        if s not in ("http", "https"):
            die("实例 %s 的 scheme=%r 不认识，只支持 http / https"
                % (i.get("label"), i.get("scheme")))
        i["scheme"] = s
        i.setdefault("insecure", bool(getattr(a, "insecure", False)))
    return insts


def backup(inst_key, r, raw, snap, client=None):
    d = os.path.join(ST_DIR, "backup", inst_key.replace(":", "_"))
    os.makedirs(d, exist_ok=True)
    out = {}
    if client is not None and getattr(client, "kind", "") == "qb":
        try:                                         # qB 能导出旧种子的原始文件，回灌时最忠实
            ob = client.dump(r["id"])
            if ob and len(ob) > 60:
                p0 = os.path.join(d, r["hash"] + ".old.torrent")
                open(p0, "wb").write(ob)
                out["old_torrent"] = p0
        except Exception:
            pass
    if raw:
        p = os.path.join(d, r["hash"] + ".new.torrent")
        open(p, "wb").write(raw)
        out["new_torrent"] = p
    p2 = os.path.join(d, r["hash"] + ".state.json")
    json.dump(snap, open(p2, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    out["state"] = p2
    return out


def cmd_apply(a):
    L = load_ledger()
    idx = load_torrent_index(a)
    if not a.old:
        die("要指定 --old（用来认出哪些种子该换 tracker）")
    total_ok = total_bad = total_skip = 0
    insts = load_instances(a)
    for inst in insts:
        key = inst["label"]
        try:
            c = connect(inst)
        except Exception as e:
            print("%s 连不上：%s" % (key, str(e)[:70]))
            continue
        cp = c.caps()
        bk = scan_backups()
        if bk:
            lost, _nb = missing_rows(c, inst, bk, single=(len(insts) == 1))
            if lost:
                if not getattr(a, "auto_fix", True):
                    print("  ⚠ %d 个有备份的条目已不在列表（多半是旧版误删的）——"
                          "给了 --no-auto-fix，不自动找回（要修跑 repair）" % len(lost))
                elif not a.yes:
                    print("  ⚠ %d 个有备份的条目已不在列表（多半是旧版误删的）——"
                          "--yes 真执行时会自动找回：只加不删、认出数据零回流才做种"
                          % len(lost))
                else:
                    print("  ⚠ %d 个有备份的条目已不在列表（多半是旧版误删的）→ "
                          "开工先自动找回（暂停+跳校验加回，认全才恢复做种）" % len(lost))
                    fx = repair_missing(c, inst, lost, a)
                    print("  自动找回：%d/%d 恢复做种" % (fx, len(lost)))
        todo = pick_targets(c, a, L, key)
        print("\n%s  %s\n  待处理 %d 个 | 跳过校验：%s" % ("=" * 56, key, len(todo), cp["note"]))
        if not todo:
            continue
        if not a.yes:
            n_ok = n_skip = n_miss = 0
            show = []
            for r in todo:
                e = idx.get(r["hash"])
                nurl = (e["meta"]["announces"] or [None])[0] if e else None
                if nurl is None:
                    flag = "✗ 配不到新种子"; n_miss += 1
                elif nurl in r["trackers"] and r["done"]:
                    flag = "= 已是新 tracker，会跳过"; n_skip += 1
                else:
                    flag = "→ 将处理"; n_ok += 1
                if flag != "= 已是新 tracker，会跳过" and len(show) < 25:
                    show.append((flag, r))
            for flag, r in show:
                print("  %-22s %s  %-46s %7.2f GB" % (flag, r["hash"][:12],
                                                      (r["name"] or "")[:44],
                                                      r["size"] / 2 ** 30))
            print("  统计：将处理 %d | 会跳过(已换过) %d | 配不到新种子 %d（共 %d）"
                  % (n_ok, n_skip, n_miss, len(todo)))
            print("  [演练] 没改任何东西。确认无误后加 --yes 执行。")
            total_skip += n_ok
            continue
        L.setdefault(key, {"done": {}, "failed": {}})
        bad_streak = 0
        for i, r in enumerate(todo, 1):
            tag = "[%s %d/%d]" % (key, i, len(todo))
            bk = None
            try:
                raw, new_url, how = new_torrent_for(a, idx, r)   # 配不到就直接报错，不猜
                if new_url and new_url in r["trackers"] and r["done"]:
                    total_skip += 1
                    print("  %s = 已是新 tracker 且在做种，跳过" % tag)
                    continue
                snap = {"fetched": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "inst": key, "hash": r["hash"], "name": r["name"],
                        "old_url": r["old_url"], "new_url": new_url, "path": r["path"],
                        "category": r.get("category") or "", "tags": r.get("tags") or "",
                        "pct": round(r["pct"], 2), "status": r["status"],
                        "trackers": r["trackers"]}
                bk = backup(key, r, raw, snap, c)
                if a.dry_run:
                    raise RuntimeError("演练模式（未改动）")
                verdict, res = (do_qb if c.kind == "qb" else do_tr)(c, r, raw, new_url, a,
                                                                 False, bk=bk)
                rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inst": key,
                       "hash": r["hash"], "name": r["name"], "size": r["size"],
                       "old_id": r["id"], "new_id": res.get("new_id"),
                       "tracker_ok": res.get("tracker_ok"), "skip_check": res.get("skip_check"),
                       "why": res.get("why") or "pct=%s" % res.get("pct"),
                       "verdict": {"ok": "✓ 已换并在做种",
                                   "backflow": "⚠ 回流·已停住没再下",
                                   "incomplete": "⚠ 校验未通过·已停住不下载",
                                   "notready": "⚠ 未就绪·先停住",
                                   "hashbusy": "⚠ 同hash已存在·未动旧条目"}.get(verdict, verdict)}
                if res.get("same_hash"):
                    rec["why"] = (rec["why"] or "") + " | 同hash(先让位再加新)"
                if verdict == "ok":
                    L[key]["done"][r["hash"]] = {"name": r["name"], "new_id": res.get("new_id"),
                                                 "ts": rec["ts"], "new_url": new_url,
                                                 "backup": bk}
                    L[key]["failed"].pop(r["hash"], None)
                    total_ok += 1
                    bad_streak = 0
                    print("  %s ✓ %s  (%.1f GB, %s)  hash=%s" % (tag, (r["name"] or "")[:44],
                                                                 r["size"] / 2 ** 30, how[:22],
                                                                 r["hash"][:12]))
                else:
                    L[key]["failed"][r["hash"]] = {"name": r["name"], "why": rec["why"],
                                                   "verdict": verdict,
                                                   "new_id": res.get("new_id"), "ts": rec["ts"],
                                                   "backup": bk}
                    total_bad += 1
                    bad_streak += 1
                    print("  %s ⚠ %s | %s  hash=%s" % (tag, (r["name"] or "")[:40],
                                                       rec["why"], r["hash"][:12]))
                save_ledger(L)
                result_row(rec)
            except Exception as e:
                total_bad += 1
                bad_streak += 1
                msg = str(e)[:200]
                L[key]["failed"][r["hash"]] = {"name": r["name"], "why": msg, "ts":
                                               datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                if bk:
                    L[key]["failed"][r["hash"]]["backup"] = bk
                save_ledger(L)
                result_row({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inst": key,
                            "hash": r["hash"], "name": r["name"], "size": r["size"],
                            "old_id": r["id"], "verdict": "✗ 未改动", "why": msg})
                print("  %s ✗ %s | %s  hash=%s（回滚：rollback --hash %s）"
                      % (tag, (r["name"] or "")[:36], msg[:100], r["hash"][:12], r["hash"][:12]))
            if a.max_fail and bad_streak >= a.max_fail:
                print("  连续失败 %d 次，收工（台账已存，可修好后重跑，自动跳过已完成的）"
                      % bad_streak)
                break
            time.sleep(a.sleep)
    print("\n%s 成功 %d 异常 %d 演练待办 %d %s" % ("=" * 14, total_ok, total_bad,
                                                total_skip, "=" * 14))
    if total_bad:
        sys.exit(21)


def find_data(root, name, size, multi):
    """在 root 目录树里按 名字+大小 定位这份种子的原数据在哪。
    单文件种子 → 同名同大小那个文件的所在目录；多文件种子 → 那个同名文件夹的父目录。
    找到唯一一处才算数；有歧义宁可报出来让人自己确认，不瞎猜。"""
    hits = []
    for base, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if multi:
            if name in dirs:
                try:
                    if os.listdir(os.path.join(base, name)):
                        hits.append(base)
                except OSError:
                    pass
            elif name in fs:                      # 站点把它做成了单文件也有可能，认下来
                try:
                    if os.path.getsize(os.path.join(base, name)) == size:
                        hits.append(base)
                except OSError:
                    pass
        elif name in fs:
            try:
                if os.path.getsize(os.path.join(base, name)) == size:
                    hits.append(base)
            except OSError:
                pass
        if len(hits) > 1:
            break
    if not hits:
        return None, "在 %s 下没找到数据（%s）" % (root, name[:40])
    if len(hits) > 1:
        return None, "数据位置有歧义（%s 等两处），请人工确认" % "；".join(hits[:2])
    return hits[0], ""


def cmd_recover(a):
    """两头空/误删/换客户端之后，把你的种子找回来重新做种。

    --torrents-dir 里放站点的新 .torrent（和当初换 tracker 用的是同一批文件）；
    --root 指向你数据所在的目录树。命令逐条核对：列表里没有的 hash → 按种子内的
    名字+大小找到原数据 → 暂停+跳过校验加回 → 确认 100% 且零回流才启动做种；
    认不出或找不到数据的一律列出来，绝不重新下载、绝不删除任何东西。"""
    if not a.torrents_dir or not os.path.isdir(a.torrents_dir):
        die("recover 需要 --torrents-dir（站点下载的新种子目录，和换 tracker 时同一批）")
    root = getattr(a, "root", "") or ""
    if not root or not os.path.isdir(root):
        die("recover 需要 --root <数据目录>（你的种子数据放在哪个目录树下）")
    idx = load_torrent_index(a)
    if not idx:
        die("--torrents-dir 里没有能读的 .torrent")
    now = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    done_hashes = set()
    for inst in load_instances(a):
        c = connect(inst)
        have = client_hashes(c)
        miss = [e for h, e in idx.items()
                if e["meta"]["hash"] == h and h not in have and h not in done_hashes]
        print("%s：共 %d 份新种子，这台客户端缺 %d 份"
              % (inst["label"], len({id(e) for e in idx.values()}), len(miss)))
        for e in miss:
            m = e["meta"]
            done_hashes.add(m["hash"])
            multi = not (len(m["files"]) == 1 and m["files"][0] == m["name"])
            tag = "%s %s" % (m["hash"][:12], (m["name"] or "")[:44])
            sp, note = find_data(root, m["name"], m["size"], multi)
            if a.dry_run:
                print("  %s %s %s" % (tag, ("→ " + sp) if sp else "✗ " + note, "（演练）"))
                continue
            if not sp:
                print("  %s ✗ %s（没动它；可手动添加种子指向数据目录并勾跳过校验）" % (tag, note))
                result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                            "name": m["name"], "size": m["size"],
                            "verdict": "✗ 没找到数据", "why": note})
                continue
            try:
                if c.kind == "qb":
                    c.add(e["raw"], sp, paused=True, skip_check=True)
                    nid = _qb_wait_id(c, m["hash"], tries=max(2, int(getattr(a, "wait_limit", 30))))
                    if not nid:
                        raise RuntimeError("qB 拒收这份种子")
                    st = _qb_watch(c, nid, a)
                    if st and st["done"] and (st.get("downloaded") or 0) == 0:
                        try:
                            c.start(nid)
                        except Exception:
                            pass
                        print("  %s ✓ 已找回并继续做种（%s，跳过校验认全）" % (tag, sp))
                        result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                                    "name": m["name"], "size": m["size"], "path": sp,
                                    "verdict": "✓ 已找回", "why": ""})
                    elif st and (st.get("downloaded", 0) > 0 or st.get("downloading")):
                        try:
                            c.stop(nid)
                        except Exception:
                            pass
                        print("  %s ⚠ 没认出文件、它开始要下载了 —— 已暂停在 %s（不下载也不做种，"
                              "请人工核对数据位置）" % (tag, sp))
                        result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                                    "name": m["name"], "size": m["size"], "path": sp,
                                    "verdict": "⚠ 没认全已暂停", "why": str(st)[:120]})
                    else:
                        print("  %s ⚠ 状态拿不到/没到 100%%，留在暂停 %s" % (tag, sp))
                        result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                                    "name": m["name"], "size": m["size"], "path": sp,
                                    "verdict": "⚠ 状态异常已暂停", "why": str(st)[:120]})
                else:
                    tid = c.add(e["raw"], sp, paused=True)
                    ok, st = None, None
                    for _ in range(max(3, int(getattr(a, "wait_limit", 30)))):
                        st = c.state(tid)
                        if st and st["done"] and (st.get("downloaded") or 0) == 0:
                            ok = True
                            break
                        if st and (st.get("downloaded") or 0) > 0:
                            ok = False
                            break
                        time.sleep(1)
                    if ok:
                        c.start(tid)
                        print("  %s ✓ 已找回并继续做种（%s）" % (tag, sp))
                        result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                                    "name": m["name"], "size": m["size"], "path": sp,
                                    "verdict": "✓ 已找回", "why": ""})
                    else:
                        try:
                            c.call("torrent-stop", {"ids": [tid]})
                        except Exception:
                            pass
                        print("  %s ⚠ 没能确认 100%%/零回流，已暂停在 %s（交人工核对）" % (tag, sp))
                        result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                                    "name": m["name"], "size": m["size"], "path": sp,
                                    "verdict": "⚠ 没认全已暂停", "why": str(st)[:120]})
            except Exception as ex:
                print("  %s ✗ 找回失败：%s（数据在 %s 没动）" % (tag, str(ex)[:100], sp))
                result_row({"ts": now(), "inst": inst["label"], "hash": m["hash"],
                            "name": m["name"], "size": m["size"], "path": sp,
                            "verdict": "✗ 找回失败", "why": str(ex)[:120]})


def set_state_dir(d):
    """把台账/备份挪到指定目录。旧版和新版经常不在同一个文件夹——state/backup
    躺在旧文件夹里，新版得能用 --state-dir 指过去，不然一键修复找不到备份。"""
    global ST_DIR, LEDGER
    if d:
        ST_DIR = os.path.abspath(d)
        LEDGER = os.path.join(ST_DIR, "ledger.json")
        os.makedirs(ST_DIR, exist_ok=True)


def discover_state_dirs():
    """--state-dir 没给时的自动发现：自己脚边 + 同级的兄弟文件夹 + 当前目录，
    凡是含 state/backup 的都算候选（典型场景：旧版文件夹和新版文件夹挨着放）。"""
    cands = []
    seen = set()

    def probe(base):
        d = os.path.join(base, "state")
        if os.path.isdir(os.path.join(d, "backup")) and d not in seen:
            seen.add(d)
            cands.append(d)

    probe(HERE)
    try:
        parent = os.path.dirname(HERE)
        for fn in sorted(os.listdir(parent)):
            probe(os.path.join(parent, fn))
    except OSError:
        pass
    probe(os.getcwd())
    return cands


GUI = os.path.join(HERE, "guiconfig.json")
PICKED = []                       # --pick-state 指定的目录（启动时归一化成 state 目录）


def normalize_pick(d):
    """用户挑什么都行：旧工具文件夹、它旁边的 state 文件夹、甚至旧 exe 文件本身
    ——归一化成能用的 state 目录；里面没有 backup 就返回空串。"""
    d = os.path.abspath(os.path.expanduser((d or "").strip().strip('"').strip("'")))
    if not d:
        return ""
    if os.path.isfile(d):
        d = os.path.dirname(d)
    bases = [d]
    if os.path.basename(d) == "state":
        bases.append(os.path.dirname(d))
    for b in bases:
        sd = os.path.join(b, "state")
        if os.path.isdir(os.path.join(sd, "backup")):
            return sd
        if os.path.basename(b) == "state" and os.path.isdir(os.path.join(b, "backup")):
            return b
    return ""


def add_state_dir(d):
    """界面「选旧工具目录」用：把用户挑的目录归一化后写进 guiconfig.json 记住。
    返回归一化出的 state 目录；找不到就抛 ValueError（界面直接显示这句话）。"""
    p = normalize_pick(d)
    if not p:
        raise ValueError("%s 里面没找到 state/backup——挑旧版工具所在的那个文件夹"
                         "（或直接挑旧 exe）" % d)
    cfg = {}
    try:
        cfg = json.load(open(GUI, encoding="utf-8"))
    except Exception:
        pass
    lst = [str(x) for x in (cfg.get("state_dirs") or [])]
    if p not in lst:
        lst.append(p)
    cfg["state_dirs"] = lst
    try:
        with open(GUI, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.chmod(GUI, 0o600)
    except OSError as e:
        raise ValueError("写 guiconfig.json 失败：%s" % e)
    return p


def extra_state_dirs():
    """guiconfig.json 里用户攒下的旧备份目录（只留还在的）。"""
    out = []
    try:
        cfg = json.load(open(GUI, encoding="utf-8"))
    except Exception:
        return out
    for x in cfg.get("state_dirs") or []:
        x = str(x)
        p = x if os.path.isdir(os.path.join(x, "backup")) else normalize_pick(x)
        if p and p not in out:
            out.append(p)
    return out


def backup_dirs():
    """回滚/修复/列表要找备份的地方：自己的 state → 界面里选过的旧工具目录 →
    --pick-state 这次指定的，按顺序去重。（旁边兄弟文件夹的自动发现留给
    repair 兜底，不进列表，免得把无关文件夹的备份混进来。）"""
    ds = []
    for d in [ST_DIR] + extra_state_dirs() + list(PICKED):
        if d and os.path.isdir(d) and d not in ds:
            ds.append(d)
    return ds


def import_old(src, dry=False):
    """一键把旧版工具的家当搬进自己家：state/backup 的备份文件、guiconfig 的
    客户端实例、ledger 台账。导完之后旧版文件夹就可以整个不管了，回滚/修复
    再也不用指旧目录。两头都不破坏：文件只复制（旧版原样留着），自己家已有
    的同名备份不覆盖；实例按 类型+协议+地址+端口+账号 去重；台账只补缺，
    自己家已记的条目优先。"""
    sd = normalize_pick(src)
    if not sd:
        raise ValueError("%s 不像旧版工具文件夹（里面找不到 state/backup）" % src)
    rep = {"from": sd, "files": 0, "same": 0, "inst": 0, "ledger": 0,
           "tdir": "", "sdirs": 0}
    ob = os.path.join(sd, "backup")
    if os.path.isdir(ob):
        for label in sorted(os.listdir(ob)):
            d = os.path.join(ob, label)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith((".new.torrent", ".old.torrent", ".state.json")):
                    continue
                dst = os.path.join(ST_DIR, "backup", label, fn)
                if os.path.exists(dst):
                    rep["same"] += 1
                    continue
                rep["files"] += 1
                if not dry:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(os.path.join(d, fn), dst)
    cfg_old = {}
    try:
        cfg_old = json.load(open(os.path.join(os.path.dirname(sd), "guiconfig.json"),
                                 encoding="utf-8"))
    except Exception:
        pass
    old_insts = [i for i in (cfg_old.get("instances") or [])
                 if isinstance(i, dict) and i.get("host") and i.get("port")]

    # 旧版界面上填过的配置一并搬：新种子目录（自家空的才补）、远处备份目录清单（去重合并）
    ov = ""
    of = cfg_old.get("form")
    if isinstance(of, dict) and isinstance(of.get("tdir"), str):
        ov = of.get("tdir").strip()
    elif isinstance(cfg_old.get("tdir"), str):
        ov = cfg_old.get("tdir").strip()
    try:
        cfgc = json.load(open(GUI, encoding="utf-8"))
        if not isinstance(cfgc, dict):
            cfgc = {}
    except Exception:
        cfgc = {}
    changed = False
    if ov and not str((cfgc.get("form") or {}).get("tdir") or "").strip():
        cfgc.setdefault("form", {})["tdir"] = ov
        rep["tdir"] = ov
        changed = True
    lst = [str(x) for x in (cfgc.get("state_dirs") or [])]
    mine = set()
    for x in lst:
        try:
            mine.add(os.path.realpath(x))
        except Exception:
            mine.add(x)
    for d in (cfg_old.get("state_dirs") or []):
        if not isinstance(d, str) or not d.strip():
            continue
        try:
            rp = os.path.realpath(d)
        except Exception:
            rp = d
        if rp in mine:
            continue
        lst.append(d)
        mine.add(rp)
        rep["sdirs"] += 1
        changed = True
    cfgc["state_dirs"] = lst
    if changed and not dry:
        with open(GUI, "w", encoding="utf-8") as fh:
            json.dump(cfgc, fh, ensure_ascii=False, indent=1)
        os.chmod(GUI, 0o600)

    def ikey(i):
        return (str(i.get("kind", "")), str(i.get("scheme", "http")),
                str(i.get("host")), str(i.get("port")), str(i.get("user", "")))
    if old_insts:
        try:
            cfg = json.load(open(GUI, encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        mine = cfg.setdefault("instances", [])
        have = {ikey(i) for i in mine if isinstance(i, dict)}
        for i in old_insts:
            if ikey(i) in have:
                continue
            mine.append(i)
            have.add(ikey(i))
            rep["inst"] += 1
        if rep["inst"] and not dry:
            with open(GUI, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            os.chmod(GUI, 0o600)
    try:
        L_old = json.load(open(os.path.join(sd, "ledger.json"), encoding="utf-8"))
    except Exception:
        L_old = {}
    if isinstance(L_old, dict) and L_old:
        L = load_ledger()
        for label, blk in L_old.items():
            if not isinstance(blk, dict):
                continue
            tgt = L.setdefault(label, {})
            for side in ("done", "failed"):
                for h, rec in ((blk.get(side) or {})).items():
                    dst_side = tgt.setdefault(side, {})
                    if h in dst_side:
                        continue
                    dst_side[h] = rec
                    rep["ledger"] += 1
        if rep["ledger"] and not dry:
            save_ledger(L)
    return rep


def cmd_import(a):
    """import --from <旧版工具文件夹> [--dry-run]：一键导入旧版备份/实例/台账。"""
    if not a.import_from:
        die("import 需要 --from <旧版工具文件夹>（也可以直接指旧 exe 或它的 state "
            "目录，可一次给多个）")
    tot = {"files": 0, "same": 0, "inst": 0, "ledger": 0}
    for src in a.import_from:
        try:
            rep = import_old(src, dry=a.dry_run)
        except ValueError as e:
            die(str(e))
        for k in tot:
            tot[k] += rep[k]
        print("%s%s：备份 +%d（已有一致的跳过 %d）、客户端实例 +%d、台账条目 +%d、备份目录 +%d%s"
              % ("（演练）" if a.dry_run else "✓ ", rep["from"],
                 rep["files"], rep["same"], rep["inst"], rep["ledger"], rep["sdirs"],
                 ("、新种子目录→%s" % rep["tdir"]) if rep.get("tdir") else ""))
    if a.dry_run:
        print("演练：上面是将要导入的内容，没真写任何东西；去掉 --dry-run 再跑一遍就真导入")
        return
    print("✓ 导入完成（共 备份 %d、实例 %d、台账 %d）。旧版文件夹可以整个不管了，"
          "回滚/修复/列表都用新版自己的 state" % (tot["files"], tot["inst"], tot["ledger"]))


def missing_rows(c, inst, rows, single=False):
    """该客户端列表里缺了哪些有备份的条目（旧版两头空误删的典型现场）。
    返回（缺失行, 该实例备份总数）；single=只配了一台实例时目录名对不上也认账。"""
    key = inst["label"].replace(":", "_")
    have = client_hashes(c)
    keyrows = [r for r in rows if r["inst"] == key]
    if not keyrows and single:
        keyrows = rows
    return [r for r in keyrows if r["hash"] not in have], len(keyrows)


def repair_missing(c, inst, mine, a):
    """把有备份却已不在列表的条目加回来（只加不删、绝不重新下载）：
    暂停+跳过校验 → 100% 认出数据且零回流才启动做种，认不出留暂停。
    返回恢复做种份数。repair 命令和 apply 开工自检共用这套。"""
    key = inst["label"].replace(":", "_")
    now = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fixed = 0
    for r in mine:
        h = r["hash"]
        d = r.get("dir") or os.path.join(ST_DIR, "backup", key)
        p = os.path.join(d, h + (".old.torrent" if a.to_old else ".new.torrent"))
        if not os.path.exists(p):
            p = os.path.join(d, h + ".new.torrent")
        if not os.path.exists(p):
            print("  %s %s ✗ 备份文件不全（new/old 都没有）" % (h[:12], (r["name"] or "")[:40]))
            continue
        try:
            st = json.load(open(os.path.join(d, h + ".state.json"), encoding="utf-8"))
        except Exception:
            st = {}
        url = st.get("old_url") if a.to_old else st.get("new_url")
        raw = open(p, "rb").read()
        if url:
            try:
                raw = rewrite_announce(raw, url)
            except Exception:
                pass
        tag = "%s %s" % (h[:12], (r["name"] or "")[:40])
        if a.dry_run:
            print("  %s → 演练：将从 %s 加回（%s）" % (tag, os.path.basename(p), st.get("path") or "?"))
            continue
        try:
            if c.kind == "qb":
                c.add(raw, st.get("path"), paused=True, skip_check=True,
                      category=st.get("category") or "", tags=st.get("tags") or "")
                nid = _qb_wait_id(c, h, tries=max(2, int(getattr(a, "wait_limit", 30))))
                if not nid:
                    raise RuntimeError("qB 拒收这份种子")
                w = _qb_watch(c, nid, a)
                if w and w["done"] and (w.get("downloaded") or 0) == 0:
                    try:
                        c.start(nid)
                    except Exception:
                        pass
                    print("  %s ✓ 已修复，继续做种" % tag)
                    fixed += 1
                    result_row({"ts": now(), "inst": inst["label"], "hash": h,
                                "name": r["name"], "verdict": "✓ 一键修复", "why": ""})
                else:
                    if w and (w.get("downloaded", 0) > 0 or w.get("downloading")):
                        try:
                            c.stop(nid)
                        except Exception:
                            pass
                    print("  %s ⚠ 没认出数据，已留在暂停（不下载），请人工核对路径" % tag)
                    result_row({"ts": now(), "inst": inst["label"], "hash": h,
                                "name": r["name"], "verdict": "⚠ 未认全已暂停",
                                "why": str(w)[:120]})
            else:
                tid = c.add(raw, st.get("path"), paused=True)
                ok, w = None, None
                for _ in range(max(3, int(getattr(a, "wait_limit", 30)))):
                    w = c.state(tid)
                    if w and w["done"] and (w.get("downloaded") or 0) == 0:
                        ok = True
                        break
                    if w and (w.get("downloaded") or 0) > 0:
                        break
                    time.sleep(1)
                if ok:
                    c.start(tid)
                    print("  %s ✓ 已修复，继续做种" % tag)
                    fixed += 1
                    result_row({"ts": now(), "inst": inst["label"], "hash": h,
                                "name": r["name"], "verdict": "✓ 一键修复", "why": ""})
                else:
                    try:
                        c.call("torrent-stop", {"ids": [tid]})
                    except Exception:
                        pass
                    print("  %s ⚠ 没认出数据，已留在暂停（不下载），请人工核对路径" % tag)
                    result_row({"ts": now(), "inst": inst["label"], "hash": h,
                                "name": r["name"], "verdict": "⚠ 未认全已暂停",
                                "why": str(w)[:120]})
        except Exception as e:
            print("  %s ✗ 修复失败：%s（数据没动）" % (tag, str(e)[:100]))
            result_row({"ts": now(), "inst": inst["label"], "hash": h,
                        "name": r["name"], "verdict": "✗ 修复失败", "why": str(e)[:120]})
    return fixed


def cmd_repair(a):
    """一键修复：备份在（含旧版文件夹里的）、条目却不在客户端了——批量找回来。
    用备份里的 .torrent 暂停+跳过校验加回 → 认出数据(100%)且零回流才启动做种；
    认不出的留在暂停并报告。只加不删，绝不重新下载。备份躺在旧版文件夹也不用搬：
    --state-dir 指过去，或者它就放在新版旁边的兄弟文件夹里会被自动找到。"""
    insts = load_instances(a)
    rows = scan_backups()
    if not rows:
        c = discover_state_dirs()
        if len(c) == 1:
            set_state_dir(c[0])
            rows = scan_backups()
            log("自动找到备份目录：%s" % c[0])
        elif len(c) > 1:
            print("找到多个候选 state 目录，用 --pick-state 指定旧工具所在的文件夹（也可多选）：")
            for d in c:
                print("  " + d)
            die("state 目录有歧义")
    if not rows:
        die("没有备份可修复（state/backup 是空的；若备份在旧版工具文件夹里，"
            "用 --pick-state <旧版文件夹> 指过来，界面里也有「选旧工具目录」）")
    fixed = 0
    for inst in insts:
        c = connect(inst)
        mine, n = missing_rows(c, inst, rows, single=(len(insts) == 1))
        print("%s：备份 %d 份，其中列表里缺 %d 份"
              % (inst["label"], n, len(mine)))
        fixed += repair_missing(c, inst, mine, a)
    if a.dry_run:
        print("演练模式，什么都没改。去掉 --dry-run 就是真修复。")
    else:
        print("修复完成：%d 份恢复做种；⚠ 的条目都停在暂停态，不会乱下载" % fixed)


def cmd_status(a):
    L = load_ledger()
    tot = bad = 0
    for k, v in sorted(L.items()):
        tot += len(v.get("done", {}))
        bad += len(v.get("failed", {}))
        print("%-28s 已完成 %-4d 待重试 %-3d" % (k, len(v.get("done", {})), len(v.get("failed", {}))))
    print("-" * 50)
    print("累计成功 %d，待重试 %d（台账 %s）" % (tot, bad, LEDGER))
    if bad:
        print("\n待重试明细：")
        for k, v in sorted(L.items()):
            for h, e in v.get("failed", {}).items():
                print("  %-24s %s  %-32s %s" % (k, h[:12], (e.get("name") or "")[:30],
                                                (e.get("why") or "")[:60]))


def client_hashes(c):
    """这台客户端现有条目的 hash 集合（v1 infohash 优先）"""
    if c.kind == "qb":
        return {(t.get("infohash_v1") or t.get("hash") or "").lower() for t in c.torrents()}
    return {(t.get("hashString") or "").lower() for t in
            c.call("torrent-get", {"fields": ["id", "hashString"]}, timeout=900).get("torrents", [])}


def scan_backups(prefix=""):
    """扫 backup/<实例>/ 列出所有能回滚的种子（hash、名字、状态）。
    找的地方不止自己家：旁边兄弟文件夹的 state、界面里选过的旧工具目录、
    --pick-state 指定的，全算（backup_dirs()），同 hash 就近的优先。
    回滚不该逼用户记 40 位 hash——界面从这里列出来选，命令行能贴前缀。"""
    rows = []
    seen = set()
    L = load_ledger()
    for sd in backup_dirs():
        root = os.path.join(sd, "backup")
        try:
            labels = sorted(os.listdir(root))
        except Exception:
            labels = []
        for label in labels:
            d = os.path.join(root, label)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith((".new.torrent", ".old.torrent")):
                    continue
                h = fn.split(".")[0]
                if prefix and not h.startswith(prefix):
                    continue
                if (label, h) in seen:
                    continue
                seen.add((label, h))
                st = {}
                try:
                    st = json.load(open(os.path.join(d, h + ".state.json"),
                                        encoding="utf-8"))
                except Exception:
                    pass
                v = L.get(label) or L.get(label.replace("_", ":", 1)) or {}
                rec = (v.get("failed", {}) or {}).get(h) or (v.get("done", {}) or {}).get(h) or {}
                rows.append({"inst": label, "hash": h, "dir": d,
                             "name": st.get("name") or rec.get("name") or "",
                             "ts": st.get("fetched") or rec.get("ts") or "",
                             "failed": h in (v.get("failed", {}) or {}),
                             "has_new": os.path.exists(os.path.join(d, h + ".new.torrent")),
                             "has_old": os.path.exists(os.path.join(d, h + ".old.torrent"))})
    return rows


def print_backups(rows, head=""):
    if head:
        print(head)
    if not rows:
        print("  （%s 下没有任何备份：没跑过 apply 就没有可回滚的东西）"
              % os.path.join(ST_DIR, "backup"))
        return
    for r in rows:
        print("  %s  %-6s %-38s %s  %s%s" % (
            r["hash"], "待重试" if r["failed"] else "已完成", (r["name"] or "")[:38],
            r["ts"], "new" if r["has_new"] else "", "/old" if r["has_old"] else ""))


def cur_view(c, ih):
    """客户端里同 hash 条目的现状快照（回滚前比对用）；没有该条目返回 None。"""
    if c.kind == "qb":
        row = None
        for t in c.torrents():
            if (t.get("infohash_v1") or t.get("hash", "")).lower() == ih.lower():
                row = t
                break
        if row is None:
            return None
        hh = row.get("infohash_v1") or row.get("hash")
        stt = row.get("state") or ""
        return {"path": row.get("save_path") or "",
                "category": row.get("category") or "", "tags": row.get("tags") or "",
                "urls": c.trackers(hh) or [],
                "paused": stt.startswith("paused") or stt.startswith("stopped")}
    tid = c.find(ih)
    if tid is None:
        return None
    t = c.detail(tid)
    if t is None:
        return None
    anns = [x.get("announce") for x in t.get("trackers", [])
            if (x.get("announce") or "").strip()]
    return {"path": t.get("downloadDir") or "", "labels": t.get("labels") or [],
            "urls": anns, "paused": t.get("status") == TR_STOPPED}


def rollback_diff(c, ih, st, url):
    """比对「按备份要还原成的样子」和「客户端现有条目」，返回出入清单
    [(字段, 目标值, 现状值)]；客户端没有该条目返回 None。"""
    cur = cur_view(c, ih)
    if cur is None:
        return None

    def np_(p):
        return os.path.normcase(os.path.normpath(p)) if p else ""
    out = []
    if st.get("path") and np_(cur["path"]) != np_(st["path"]):
        out.append(("保存路径", st["path"], cur["path"]))
    if url and url not in cur["urls"]:
        out.append(("tracker", url, " / ".join(cur["urls"])))
    if c.kind == "qb":
        if (st.get("category") or "") != cur["category"]:
            out.append(("分类", st.get("category") or "（无）", cur["category"] or "（无）"))
        if (st.get("tags") or "") != cur["tags"]:
            out.append(("标签", st.get("tags") or "（无）", cur["tags"] or "（无）"))
    else:
        want = {s.strip() for s in str(st.get("tags") or "").split(",") if s.strip()}
        have = {str(x) for x in cur.get("labels") or []}
        if want and not want <= have:
            out.append(("labels", ",".join(sorted(want)), ",".join(sorted(have)) or "（无）"))
    if cur["paused"]:
        out.append(("状态", "回滚后要开始做种", "当前是暂停的"))
    return out


def _tr_addback_seed(c, raw, path, a):
    """TR 加回的安全走法：暂停+限速加入 → 启动读盘校验 → 100% 才放开做种；
    一旦回流（真在下载）立刻停住。返回 (id, seeded|backflow|checking)。"""
    nid = c.add(raw, path, paused=True)
    try:
        c.dl_cap(nid, 1)
    except Exception:
        pass
    try:
        c.start(nid)
    except Exception:
        pass
    gap = float(getattr(a, "poll", 5) or 5)
    lim = gap * max(1, int(getattr(a, "wait_limit", 30) or 30))
    waited = 0.0
    while waited <= lim:
        s2 = c.state(nid) or {}
        if s2.get("done"):
            try:
                c.dl_cap(nid, 0)
            except Exception:
                pass
            return nid, "seeded"
        if s2.get("downloading"):
            try:
                c.stop(nid)
            except Exception:
                pass
            return nid, "backflow"
        time.sleep(gap)
        waited += gap
    return nid, "checking"


def cmd_rollback(a):
    """用 state/backup 里的备份把某个种子加回来（数据从未被删）。
    --hash 给完整 hash 或至少 6 个字符的前缀即可；不给 --hash 就列出全部可回滚的。"""
    if not a.hash:
        print_backups(scan_backups(), "可回滚的种子（hash 整串或前 6 位以上都行，界面上点一下就填进框）：")
        die("rollback 需要 --hash：从上面列表里挑一个，hash 贴整串或开头几位都行")
    want = a.hash.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,40}", want):
        die("hash 应该是 6~40 位的 0-9/a-f（完整 info_hash 或它的前缀）")
    hits = scan_backups(want)
    pairs = sorted({(r["inst"], r["hash"]) for r in hits})
    if not pairs:
        print_backups(scan_backups(),
                      "没有以 %s 开头的备份。当前可回滚的种子：" % want)
        die("找不到匹配 %s 的备份（对照上面列表重新给 --hash）" % want)
    if len(pairs) > 1:
        print("前缀 %s 匹配到 %d 个种子，请多输几位区分：" % (want, len(pairs)))
        for inst, h in pairs:
            print("  %s  %s" % (h, inst))
        die("hash 前缀太短，匹配到多个")
    key, h = pairs[0]
    row = next(r for r in hits if (r["inst"], r["hash"]) == (key, h))
    insts = load_instances(a)
    inst = next((i for i in insts if i["label"].replace(":", "_") == key), None)
    if inst is None and len(insts) == 1:
        inst = insts[0]          # 只配了一台：备份目录名对不上也直接用它
    if inst is None:
        die("备份目录 %s 对不上任何已配置实例——把这台实例加回配置文件再回滚" % key)
    d = row.get("dir") or os.path.join(ST_DIR, "backup", key)
    p = os.path.join(d, h + (".old.torrent" if a.to_old else ".new.torrent"))
    if not os.path.exists(p):
        p = os.path.join(d, h + ".new.torrent")
    c = connect(inst)
    raw = open(p, "rb").read()
    m = torrent_info(raw)
    st = json.load(open(os.path.join(d, h + ".state.json"), encoding="utf-8"))
    url = st.get("old_url") if a.to_old else st.get("new_url")
    if url:
        raw = rewrite_announce(raw, url)
    diffs = rollback_diff(c, h, st, url)
    if diffs is not None:                      # 同 hash 条目已经躺在客户端里
        if not diffs:
            print("✓ %s  hash=%s  客户端里已有一条一模一样的（tracker/路径/分类/标签都对得上），"
                  "不用回滚" % (m["name"][:44], h[:12]))
            return
        print("⚠ %s  hash=%s  客户端已有同 hash 条目，按备份还原会和它不一致："
              % (m["name"][:44], h[:12]))
        for k, a_, b_ in diffs:
            print("    %s：备份要=%s ｜ 现在是=%s" % (k, str(a_)[:72], str(b_)[:72]))
        if getattr(a, "rb_keep", False):
            print("  → 按你的选择：条目原样保留，什么都没改（保持不动）")
            return
        if not getattr(a, "rb_force", False):
            die("要覆盖：rollback --hash %s --force（删掉现有条目、数据文件不动，按备份重新加回）\n"
                "      不动它：加 --keep（网页/窗口界面在回滚处有对应勾选）" % h[:12])
        try:
            if c.kind == "qb":
                c.remove(h)
            else:
                tid = c.find(h)
                if tid is not None:
                    c.remove(tid)
        except Exception as e:
            die("覆盖失败：现有条目删不掉 —— %s" % str(e)[:120])
        log("  覆盖：已删掉现有条目（数据文件没动）")
    if c.kind == "qb":
        c.add(raw, st.get("path"), paused=False, category=st.get("category") or "",
              tags=st.get("tags") or "", skip_check=True)
    else:
        nid, mode2 = _tr_addback_seed(c, raw, st.get("path"), a)
        log("  已加回 TR id=%s" % nid)
        if mode2 == "backflow":
            print("★ %s  hash=%s  盘上数据有缺口，校验中已开始回流 —— 已立刻停住，"
                  "没有让它继续下载，回滚未完成（数据补全后再回滚一次）"
                  % (m["name"][:44], h[:12]))
            return
        if mode2 == "checking":
            print("⚠ %s  hash=%s  已按备份加回，但还没校验完（期间限速 1KB/s 下不动东西，"
                  "符合预期）；校验到 100%% 后自行开始做种即可" % (m["name"][:44], h[:12]))
            return
    print("✓ %s  hash=%s  已按 %s 加回（tracker=%s）"
          % (m["name"][:44], h[:12], key, (url or "")[:40]))


def build_parser():
    ap = argparse.ArgumentParser(
        prog="tracker-replace",
        description="tracker替换 —— 批量替换 tracker（qBittorrent / Transmission）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  # 1) 体检：连上看看是什么客户端、能不能跳过校验、有多少要换
  python3 tracker_replace.py doctor -c config.json --old "tracker.example.com"

  # 2) 演练：列出将要处理的种子（不改任何东西）
  python3 tracker_replace.py apply -c config.json --old "tracker.example.com" \\
      --torrents-dir ./new-torrents

  # 3) 真跑（qB 免校验直接认盘；TR 会重校验但守住零回流）
  python3 tracker_replace.py apply -c config.json --old "tracker.example.com" \\
      --torrents-dir ./new-torrents --yes

  # 只换某一台 / 只换名字含关键字的 / 换完先别做种
  python3 tracker_replace.py apply --host 127.0.0.1 --port 8080 --user me \\
      --old "tracker.example.com" --new-url "https://new/announce?token=xxx" --only 片子名

  # 看台账、导出结果、把某个种子加回来
  python3 tracker_replace.py status
  python3 tracker_replace.py rollback --hash <info_hash> -c config.json
""")
    ap.add_argument("cmd", choices=["doctor", "apply", "status", "report", "rollback",
                                    "recover", "repair", "import", "gui", "ui"])
    ap.add_argument("-c", "--config", help="实例配置文件（JSON，见 config.example.json）")
    ap.add_argument("-V", "--version", action="version", version="tracker替换 " + VERSION)
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="", help="留空则读环境变量 TRREPLACE_PASSWORD")
    ap.add_argument("--kind", choices=["qb", "tr"], help="跳过自动识别")
    ap.add_argument("--scheme", choices=["http", "https"], default="http",
                    help="WebUI/RPC 协议，默认 http；https 走 TLS")
    ap.add_argument("--insecure", action="store_true",
                    help="https 时跳过证书校验（内网自签证书用，别乱开）")
    ap.add_argument("--old", default="", help="旧 tracker 的匹配子串（必给）")
    ap.add_argument("--new-url", dest="new_url", default="",
                    help="期望的新 tracker（用来校验换对了没）；不改变「必须用站点新种子文件」这件事")
    ap.add_argument("--torrents-dir", dest="torrents_dir", help="站点下载的新种子目录（按 info hash 配对）")
    ap.add_argument("--root", default="", help="recover 用：你的种子数据所在目录树（找回时按名字+大小定位）")
    ap.add_argument("--state-dir", dest="state_dir", default="",
                    help="台账/备份所在目录（默认程序旁的 state/；备份在旧版文件夹里就指过去）")
    ap.add_argument("--csv", help="CSV：torrent 列 = .torrent 路径，可选 hash/name 列")
    ap.add_argument("--only", default="", help="只处理名字或 hash 含此串的")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理几个")
    ap.add_argument("--yes", action="store_true", help="真正执行（默认演练）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="强制演练")
    ap.add_argument("--pause", action="store_true", help="换完保持暂停，不自动做种")
    ap.add_argument("--layout", default="", help="qB contentLayout：Original/NoCreateSubfolder/CreateSubfolder")
    ap.add_argument("--check-budget", dest="check_budget", type=int, default=1800,
                    help="TR 每个种子最多等多少秒读盘校验（默认 1800）")
    ap.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒")
    ap.add_argument("--wait-limit", dest="wait_limit", type=int, default=30,
                    help="qB 等条目消失/出现的秒数上限（默认 30）")
    ap.add_argument("--add-tries", dest="add_tries", type=int, default=5,
                    help="qB /torrents/add 被拒（Fails.）时的重试次数：删条目后 hash 释放有滞后（默认 5）")
    ap.add_argument("--add-gap", dest="add_gap", type=float, default=2.0,
                    help="Fails. 重试的间隔秒（默认 2.0）")
    ap.add_argument("--no-cap", dest="no_cap", action="store_true",
                    help="TR 校验期间不加 1KB/s 下载限速（默认加，防止校验失败还去下载）")
    ap.add_argument("--sleep", type=float, default=2.0, help="每个之间的间隔秒")
    ap.add_argument("--max-fail", dest="max_fail", type=int, default=5,
                    help="连续失败几个就收工，0=不限制")
    ap.add_argument("--ui-port", dest="ui_port", type=int, default=0,
                    help="ui 子命令的端口（默认自己挑空闲端口；只监听 127.0.0.1）")
    ap.add_argument("--no-browser", dest="no_browser", action="store_true",
                    help="ui 子命令不要自动开浏览器")
    ap.add_argument("--selftest", default="", help=argparse.SUPPRESS)
    ap.add_argument("--hash", default="", help="rollback 用：完整 info_hash 或至少 6 位前缀；不给则列出全部可回滚种子")
    ap.add_argument("--to-old", dest="to_old", action="store_true", help="rollback 时换回旧 tracker")
    ap.add_argument("--force", dest="rb_force", action="store_true",
                    help="rollback 用：同 hash 条目已在但字段不一致时，删掉它（数据不动）按备份强制覆盖")
    ap.add_argument("--keep", dest="rb_keep", action="store_true",
                    help="rollback 用：同 hash 条目已在但字段不一致时，明确选择不动它")
    ap.add_argument("--from", dest="import_from", nargs="+", default=[], metavar="旧目录",
                    help="import 用：旧版工具所在的文件夹（也可直接指旧 exe 或它的 state 目录，"
                         "可给多个）；一键把旧版的备份、客户端实例、台账并进新版")
    ap.add_argument("--no-auto-fix", dest="auto_fix", action="store_false",
                    help="apply 开工前默认会自动找回「有备份却已不在列表」的条目"
                         "（旧版误删的那些，只加不删、认出数据零回流才做种）；加这个关掉")
    ap.add_argument("--pick-state", dest="pick_state", nargs="+", default=[], metavar="旧目录",
                    help="rollback/recover/repair 用：旧版工具所在的文件夹（也可以直接指旧 exe"
                         " 或它的 state 目录），到里面去找备份；可给多个")
    return ap


def main():
    ap = build_parser()
    a = ap.parse_args()
    set_state_dir(getattr(a, "state_dir", ""))
    os.makedirs(ST_DIR, exist_ok=True)
    for d in getattr(a, "pick_state", []) or []:
        p = normalize_pick(d)
        if not p:
            die("--pick-state %s 里面没找到 state/backup（指旧版工具所在的文件夹，"
                "或直接指旧 exe）" % d)
        if p not in PICKED:
            PICKED.append(p)
            log("会到指定目录找备份：%s" % p)
    if a.cmd in ("gui", "ui"):
        try:
            import tracker_ui
        except ImportError:
            die("找不到 tracker_ui.py（和 tracker_replace.py 放同一目录）")
        tracker_ui.run(a)
        return
    if a.cmd == "doctor":
        if not a.old:
            die("doctor 需要 --old（要匹配哪个 tracker）")
        cmd_doctor(a)
    elif a.cmd == "apply":
        cmd_apply(a)
    elif a.cmd == "status":
        cmd_status(a)
    elif a.cmd == "rollback":
        cmd_rollback(a)
    elif a.cmd == "recover":
        cmd_recover(a)
    elif a.cmd == "repair":
        cmd_repair(a)
    elif a.cmd == "import":
        cmd_import(a)
    elif a.cmd == "report":
        print("结果表在 %s" % RESULTS)


if __name__ == "__main__":
    main()
