# tracker替换 —— 批量替换 tracker（qBittorrent / Transmission）

私有种子的 tracker URL 里带 passkey / token。站点把 announce 换了以后，客户端里那批
老种子就废了。tracker URL 在 `.torrent` 的 info 段之外，换它 **info_hash 不变**，
理论上是"改个字符串"的事 —— 但两个客户端都不给好好改，于是有了这个工具。

- **零硬编码**：不带任何 IP / 端口 / 账号，全走 `config.json` 或命令行 / 环境变量
- **环境自适应**：自动识别 qB 还是 TR、读版本号，按能力选策略
- **数据绝对不删**：`deleteFiles=false` / `delete-local-data=false`，动手前必备份种子与状态
- **可断点续跑**：台账 `state/ledger.json`，已换过的自动跳过
- 依赖：**只要 Python 3.7+，全部标准库**（bencode 内置，不用 pip 装任何东西）。
  窗口界面另需 tkinter（Windows/macOS 的 python 自带；Linux `apt install python3-tk`）。

---

## 版本

命令行 `-V`（或 `--version`）会打出当前版本，窗口标题和网页标签上也显示。

能力一览：qB / TR 批量换 tracker、免重校验直接认盘、零回流守护、台账与回滚、原生窗口 + 网页窗口 + 命令行、实例支持 http / https 协议。

### 版本修复记录

- **1.10** —— 修 qBittorrent 删大种子（Remux 等几十 GB / 文件多的）时"旧条目删了还赖在列表里"就罢工：删除前先暂停该条目（qB 停掉活跃传输后卸载明显变快），默认等待时间从 30 秒加到 120 秒（`--wait-limit` 可再加长）。触发过保护时种子无损（旧条目原样回灌），重跑即可。
- **1.9** —— 修 Windows 上跑到一半报 `WinError 10048`（端口耗尽）：原来每个请求都新开一条连接，Windows 会把关掉的连接压住约 2 分钟，批量处理几十个种子后端口撞车。改为持久连接复用（一台下载器全程基本只占 1 条连接），连接被掐或仍撞端口时自动重连并退避重试，不再中途报废。
- **1.8** —— 回滚前先比对种子现状（tracker 地址 / 保存路径 / 分类标签 / 是否暂停）：一致就报"不用回滚"；有出入默认不动手并列出差价，可选 `--force` 覆盖或 `--keep` 不动（界面有对应勾选）。修 Transmission 回滚时 `torrent-duplicate` 误报失败（同一 hash 视为成功）。Transmission 加回改为限速读盘校验，数据认全（100%、零回流）才开始做种，一旦回流立即停并报告。`import` 一并吸收旧版的"新种子目录"等设置，导入即生效。
- **1.7** —— 文档：置顶"从旧版迁移先把旧版工具文件夹并进新版"的说明，否则找回/修复/回滚没有老备份可用。
- **1.6** —— `apply` 开工前自动自检：凡"有备份、却已不在下载器列表"的条目（此前 bug 误删的）先自动找回再干活，`--no-auto-fix` 可关。
- **1.5** —— 新增 `import --from <旧版文件夹>`：一键把旧版的备份、实例配置、台账并进新版（`--dry-run` 预览，只复制不覆盖，可重复跑）；窗口和网页加"导入旧版备份/配置"按钮。
- **1.4** —— 修换 tracker 时种子"两头空"：qBittorrent 删旧条目是异步的，新条目可能加不进去、旧的却已经删了，种子凭空消失。现在添加自动重试（`--add-tries`/`--add-gap`），失败自动把旧条目原样加回，加之前先查重。回滚不用记 hash：失败行直接印出 hash 和现成的 rollback 命令，`rollback` 不带参数列全部候选、支持输入前 6 位以上，界面可从备份点选。修单文件版备份放在临时目录、重启就丢的问题。新增 `recover` 命令：连 hash 都没有时按种子名+大小在数据盘定位找回，只加不删。

---

## 30 秒上手

> **从旧版 tracker替换 迁移过来的，先做这一步**：新版自己的 state 文件夹是空的，
> 旧版的备份、实例配置、台账都在**旧版工具所在的文件夹**里 —— 不指过来，
> 开工找回 / 一键修复 / 回滚 全都没老备份可用。窗口里点「导入旧版备份/配置」
> 选旧版文件夹（命令行 `import --from <旧版文件夹>`，可先加 `--dry-run` 看清单），
> 一次并进新版，旧版文件夹之后不用管。懒得导入也行：`repair` / `rollback`
> 用 `--pick-state <旧版文件夹>` 每次指过去（就在旁边兄弟文件夹时会自动找到）。

推荐用窗口界面（不用记参数）：

```bash
python3 tracker_replace.py gui      # 原生窗口；机器没图形库/没 DISPLAY 时自动改提示用 ui
python3 tracker_replace.py ui       # 网页窗口：自己开浏览器标签页当窗口，只监听 127.0.0.1
```
Windows 双击 `tracker-replace-gui.bat`（原生窗口）或 `tracker-replace-ui.bat`（浏览器窗口）。

加实例那一栏从上到下：**协议**（http / https，WebUI 开了 TLS 或走了反向代理就选
https）、类型（留空 = 自动识别）、地址、端口、用户名、密码。https 连不上、报证书错误
时，再勾「跳过证书校验」（内网自签证书才需要，能用就别勾）。

> 协议是后补的能力：早先版本只走 http，https 的地址会被当成非法地址停掉。
> 命令行对应 `--scheme https` 与 `--insecure`；JSON 里是 `"scheme": "https"`、
> `"insecure": true`，不写就按 http、正常校验证书。

窗口里的顺序就是：加实例 → ① 体检 → ② 演练 → 勾"我已确认" → ③ 真执行 → 台账。
命令行等价：

```bash
# 1) 写配置（也可以全用命令行参数，见下）
cp config.example.json config.json && vim config.json

# 2) 体检：每台是什么客户端、能不能跳过校验、有多少要换
python3 tracker_replace.py doctor -c config.json --old "tracker.example.com"

# 3) 演练：列出将要处理的种子（什么都不改）
python3 tracker_replace.py apply -c config.json --old "tracker.example.com" \
    --torrents-dir ./新种子目录

# 4) 真跑
python3 tracker_replace.py apply -c config.json --old "tracker.example.com" \
    --torrents-dir ./新种子目录 --yes

# 5) 看结果
python3 tracker_replace.py status          # 台账概览 + 待重试明细
column -s, -t results.csv | less -S   # 逐条结果
```

不想写配置文件：

```bash
export TRREPLACE_PASSWORD='你的密码'
python3 tracker_replace.py doctor --host 127.0.0.1 --port 8080 --user me --old "tracker.example.com"
```

---

## 「跳过校验」支持版本（重点）

| 客户端 | 能否免哈希校验 | 依据 |
|---|---|---|
| **qBittorrent ≥ 3.3.13**（2017-06 起所有版本，含 4.x / 5.x） | ✅ 支持 | WebAPI `POST /torrents/add` 的 `skip_checking=true`；该参数由 qB 官方 Changelog `WEBUI: Add skip_checking and paused to /command/download`（v3.3.13）引入，API 2.x 全系列沿用 |
| qBittorrent **5.3.0** | ⚠ 有名 bug 会忽略该参数 | 上游修复见 qbittorrent-api changelog（`Fix is_skip_checking for torrents/add being ignored by qBittorrent v5.3.0 (#654)`）。工具会在这种情况下检测到"加入后仍在校验/回流"并撤销新种子 |
| **Transmission 全版本**（2.x / 3.x / 4.0.x，RPC v15–v17） | ❌ 不支持 | 官方 `docs/rpc-spec.md` 的 `torrent_add` 参数只有 `cookies / download_dir / filename / labels / metainfo / paused / peer_limit / bandwidth_priority / files_wanted / files_unwanted / priority_high / priority_low / priority_normal / sequential_download / sequential_download_from_piece` —— 任何版本都没有"跳过校验"或"种子模式"参数（libtorrent 的 `seed_mode` 从未暴露到 RPC） |

### 那 Transmission 怎么办？

只能重校验。但"重校验"不等于"重新下载"，本工具在 TR 上做的是**带零回流守护的重校验**：

1. 先把旧种子的详情、文件勾选状态、tracker、目录存成快照 + 备份 `.torrent`（备份失败就直接拒动）
2. `torrent-remove(delete-local-data=false)` —— 只删条目，盘上数据一个字节都不动
3. `torrent-add(metainfo, paused=true)` 暂停态加入，顺带把原来"不下载"的文件继续标记不下载
4. `torrent-start` 让 TR 开始读盘校验（TR 在暂停态是不校验的，必须先启动）
5. 轮询 `downloadedEver`：**只要它开始真正下东西（说明盘上数据有缺口）立刻 `torrent-stop`**，
   绝不让它把几十 GB 重下一遍；旧种子的 `.torrent` 已备份，`rollback` 子命令可原样加回
6. 校验到 100% 且 tracker 正确才算成功

真要做到"TR 也免读盘"，只有一条路：在 TR 主机上停掉服务，直接换 `config/torrents/*.torrent`
并保留 `config/resume/<hash>*.resume`（TR 会信 resume 里的进度）。那需要 TR 主机的 shell，
属于另一套做法，本工具（纯 RPC）不做，也**不建议**手滑玩。

### 为什么不能"原地改 tracker"

站点换 tracker 时给的是**另一份 .torrent 文件**：announce-list、created by、comment
都可能不同，甚至 info 段也可能重做过。所以：

- 拿旧种子改个 announce 凑出来的那份，站点不认，也不能当原种子延续 —— 工具**不做**这种事
- `/torrents/editTracker` 这类"原地替换"也不作为路径（私有种子还受 libtorrent 限制，各版本行为不一）
- 唯一路径：**加新种子 → 确认 100% 做种且零回流 → 再删旧种子**（qB 与 TR 都是这条，区别只在
  qB 能 `skip_checking` 免读盘、TR 必须读盘校验）
- 因此新种子文件必须给全：`--torrents-dir` 指向站点下载的新种子目录，工具按 **info hash** 配对。
  hash 对不上（站点重做过种子）会明确拒动，不会拿同名文件硬套 —— 那种情况换了等于重新下一遍数据

---

## 命令

| 命令 | 作用 |
|---|---|
| `doctor` | 只读体检：客户端型号/版本、是否支持跳过校验、`--old` 命中多少种子 |
| `apply` | 换 tracker。**默认演练**，必须 `--yes` 才动手。真执行前先做开工自检：发现"有备份却已不在列表"的条目（旧版两头空 bug 误删的就是这种）会先自动找回再干活——只加不删、认出数据零回流才做种；`--no-auto-fix` 可关掉 |
| `status` | 台账概览 + 待重试明细（失败的种子旧条目都还在，没丢） |
| `rollback` | 用备份把某个种子按旧/新 tracker 加回来：`rollback`（不带参数=列出全部可回滚）/ `rollback --hash <完整或前6位以上>` `[--to-old]`；界面上有"选回滚种子"按钮，不用手填 hash。加回前先比对：客户端里同 hash 条目若和备份要还原的样子一致，直接报"不用回滚"；有出入会列出差异（tracker/路径/分类/标签/暂停）让你选——`--force` 覆盖（删条目、数据不动、按备份重加）或 `--keep` 保持不动，界面/网页有对应勾选，什么都不选就停在原地不动手。备份是旧版工具留下的、文件夹又搬得远：`--pick-state <旧版工具文件夹>` 指过去（直接指旧 exe 也行，可多选），界面/网页里填路径或点"浏览"挑一次就长期记住 |
| `repair` | 一键修复：备份在、条目却不在客户端了（换 tracker 中断、误删等），批量把缺的全部加回——暂停+跳过校验认出数据（100%、零回流）才恢复做种，认不出的留暂停只报告；`--pick-state` 指旧版工具目录，放着在旁边兄弟文件夹里也会被自动找到 |
| `recover` | 种子条目从列表里丢了（换 tracker 中断、误删等）之后找回：`recover --torrents-dir <站点新种子目录> --root <数据目录>`。按 hash 找"客户端里缺的"，按名字+大小定位原数据，暂停+跳过校验加回、确认 100% 且零回流才启动；找不到数据的只报告不动手，**只加不删、绝不重新下载**。先加 `--dry-run` 看清单 |
| `import` | 一键导入旧版工具的家当：`import --from <旧版工具文件夹>`（可多个，也可直接指旧 exe 或它的 state 目录）。把旧版的 **state/backup 备份、客户端实例配置、台账** 并进新版自己的 state——导完旧版文件夹就可以整个不管，回滚/修复/列表再也不用指旧目录。文件只复制不挪动（旧版原样留着）、同名不覆盖、实例按地址去重、台账只补缺；先加 `--dry-run` 看要导多少。界面上是「导入旧版备份/配置」按钮，网页窗口里填旧目录路径点「一键导入旧版」 |
| `report` | 指出逐条结果 CSV 的位置 |

## 常用参数

| 参数 | 说明 |
|---|---|
| `-c/--config` | 实例列表 JSON；也可 `--host/--port/--user/--password`（密码可走 `TRREPLACE_PASSWORD`） |
| `--scheme` | `http`（默认）或 `https`；JSON 里写 `"scheme": "https"` 等价 |
| `--insecure` | https 时跳过证书校验，只有自签证书才用；JSON 里写 `"insecure": true` |
| `--kind qb\|tr` | 跳过自动识别（客户端很多、探测慢时用） |
| `--old` | **必给**。旧 tracker 的匹配子串，决定处理范围 |
| `--torrents-dir` | 站点下载的新种子目录。按 **info hash 精确配对**，与文件名无关（文件名被站点改过也不影响） |
| `--csv` | 不方便整理目录时用 CSV：`torrent=/路径/x.torrent`，可选 `hash=` |
| `--new-url` | 不给新种子文件、只给新 announce（此时 qB 走"导出原种子改 announce"；TR 无导出能力会明确报错） |
| `--only` / `--limit` | 只处理名字或 hash 含某串的几个、先拿几个试点 |
| `--pause` | 换完保持暂停，不自动开始做种 |
| `--check-budget` | TR 每个种子最多等待读盘校验的秒数（默认 1800s），超时报"未就绪"而不是硬删 |
| `--max-fail` | 连续失败 N 个自动收工（默认 5），防止配置错了批量翻车 |
| `--sleep` / `--poll` | 种间间隔 / 轮询间隔 |

---

## 安全机制（都在代码里，不靠自觉）

1. 默认演练，`--yes` 才执行；`--dry-run` 可强制演练
2. 数据文件永不删除（qB `deleteFiles=false`；TR `delete-local-data=false`）
3. 动手前必须成功写出备份：`state/backup/<实例>/<hash>.new.torrent` + `.state.json`
4. qB 路线：新种子确认「100% + 做种态 + 零回流」之后才删旧种子
5. TR 路线：同 hash 不能并存、私有种子禁止改 tracker，被迫先删后加 —— 所以备份/目录/文件列表
   任一项取不到就拒动，且全程盯 `downloadedEver`
6. 一旦检测到回流：qB 撤销新种子（旧的没动）；TR 立刻 `torrent-stop` 并记入待重试
7. 逐条写 `results.csv` + `tracker-replace.log` + `state/ledger.json`，中断重跑自动跳过已完成的
8. 私有种子的文件勾选（"不下载"的那些文件）会在新种子上恢复，避免换完开始补下被跳过的分卷

## 交付物

```
tracker-replace/
├── tracker_replace.py            # 全部逻辑，单文件
├── tracker_ui.py              # 窗口界面（原生 tkinter + 网页窗口两种，同一个引擎）
├── config.example.json   # 配置模板（无凭据）
├── requirements.txt      # 依赖说明（工具本身：无）
├── build_linux.sh        # Linux 打单文件 -> dist/tracker-replace
├── build_windows.bat     # Windows 打单文件 -> dist\tracker-replace.exe
├── tracker替换.sh            # Linux/macOS 直接跑源码
├── tracker-replace-gui.bat       # Windows 直接跑源码（窗口）
├── tracker-replace-ui.bat        # Windows 直接跑源码（浏览器窗口）
├── dist/tracker-replace          # Linux x86_64 单文件（已打好，无需 python 直接跑）
└── tests/selftest.py     # 仿真 qB/TR 靶子 + 72 条断言（不碰真客户端）
```

三个压缩包：
| 包 | 里面有什么 | 给谁 |
|---|---|---|
| `tracker替换-src-*.zip` | 源码 + 脚本 + 自检 | 有 Python 的机器，或以后改代码 |
| `tracker替换-linux-*.zip` | `dist/tracker-replace` 单文件 + 说明 | Linux 服务器，直接 `./tracker-replace` |
| `tracker替换-windows-kit-*.zip` | 源码 + `build_windows.bat` + 两个 .bat | Windows：先双击 .bat 直接用；要 exe 就双击 `build_windows.bat` 打一次 |

> exe 只能在 Windows 上打（PyInstaller 不支持从 Linux 交叉编译），所以 Windows 包里给的是
> 一键打包脚本 + 免打包直用的 .bat；在任意一台 Windows 上跑一次 `build_windows.bat`，
> 得到的 `dist\tracker-replace.exe` 可以单独拷去任何 Windows 机器（`--noconsole`，只出窗口不弹黑框）。

跑过之后会多出：`results.csv`、`tracker-replace.log`、`state/ledger.json`、`state/backup/…`、
`guiconfig.json`（窗口里存的东西，**里面有密码**）
（这些是你的运行数据，别提交到公开仓库；`config.json` 里有密码，同理）

## 排查

| 现象 | 原因 / 处理 |
|---|---|
| TR 返回 409 | Transmission 的 CSRF 保护，工具会自动取 `X-Transmission-Session-Id` 重发；若一直 409 检查反向代理是否吃掉这个响应头 |
| TR 加了种子但一直 0% | 暂停态不校验，必须先 `torrent-start`（工具已自动做）。TR 对新加入种子的首次校验也会把 `status` 报成 4（下载），**判回流要看 `downloadedEver`，不能只看 status** |
| qB 登录后 403 | WebUI 的 CSRF / IP 白名单（`Bypass local authentication`、`WebUI\CSRFProtection`） |
| qB 加入后仍在校验 | `skip_checking` 没生效（5.3.0 已知 bug），工具会撤销新种子并报错 |
| TR 报 `error setting announce list` | 就是"私有种子禁止改 tracker"，只能删除+重加（本工具的默认路径） |
| 换完 pct 不到 100% | 盘上数据本来就有缺口（旧种子只是靠 resume 文件里的旧进度在"假装做种"）。工具已把它停住；要么补齐缺失文件，要么把那几个文件取消勾选 |

## 实测记录（真实环境，一批数百个私有种子 / 数十 TB）

> 具体机器、站点、数量这里不写——那是你自己的运行信息，留在本地日志里就好。

- 环境：qBittorrent v4.6.x（WebAPI 2.9.3）与 Transmission 4.0.5（RPC v17），多台混跑
- 绝大多数换完并确认 100% 做种、tracker 正确、`downloadedEver=0`（零回流）
- 个别（TR）换后校验发现盘上确实缺文件（旧种子是靠 resume 假装 100%），零回流守护把它们
  停在下载前，等人工决定：补文件 或 取消勾选那几个文件
- 单台一次跑上百个种子也能全部通过，校验期间无一个开始重新下载


## 复检记录（2026-11-04，改造为窗口对话形态之后）

| 维度 | 怎么验的 | 结果 |
|---|---|---|
| 引擎逻辑 | `python3 tests/selftest.py`：本地仿真 qB WebAPI / TR RPC，把成功路径和每种失败路径（回流、校验卡住、认证失败、同 hash、加不进…）各跑一遍，逐条断言"该做的做了、不该做的没做" | 断言 72 条：通过 72，失败 0 |
| 铁律：校验没过不自动下载 / 只有校验通过才自动做种 | 同上（`QB-3/QB-4/TR-4` 等用例专门断言：校验失败或状态不对时绝不 `set-seeding`、绝不留两头空；TR 校验前先把下载限速压到 1KB/s） | 全过 |
| 原生窗口 | Xvfb 里真开窗口、真按 ①体检 ②演练 ③真执行 台账 四个按钮，日志来自窗口文本框 | 通过（体检识别 qB + skip_checking、演练零改动、真执行 ✓ 1 成功、台账累计正确） |
| 网页窗口 | 真起 `tracker_replace.py ui`（只监听 127.0.0.1），真实浏览器打开点按钮 | 通过（qB 与 TR 两台同时列出，语义正确） |
| Linux 单文件 | `dist/tracker-replace` 在 Xvfb 里真开一次窗口 + 真跑体检/演练 | 通过（tkinter 与 tracker_ui 都已打进包内，不依赖 python 环境） |
| 自检本身的坑 | 早先窗口自检抓不到日志会**假绿**，已修（补 `drain` 调度 + 空日志判失败） | 已修 |

> 说明：本轮真机（内网 / 公网实例）不可达，以上验证全部打在本地仿真靶子上；
> 真机连通后建议再跑一次 `doctor` + `apply`（演练）复核。


