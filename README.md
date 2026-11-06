# tracker替换 —— 批量替换 tracker（qBittorrent / Transmission）

私有种子的 tracker URL 里带 passkey，站点换了 announce 之后客户端里那批老种子就废了。
tracker URL 在 `.torrent` 的 info 段之外，换它 **info_hash 不变**，本质是"改个字符串"——
这个工具把它变成一键批量：按 hash 配对新旧种子，**直接原地编辑 tracker**（qB 4.1+ 官方
editTracker / TR setTrackers），不删种子、不暂停、零等待；并包办批量该有的活：自动认
客户端和版本、零回流守护、台账断点续跑、出问题一键回滚。

- **零硬编码**：不带任何 IP / 端口 / 账号，全走配置或命令行
- **环境自适应**：自动识别 qB 还是 TR、读版本号，按能力选策略
- **数据绝对不删**：`deleteFiles=false` / `delete-local-data=false`，动手前必备份
- **可断点续跑**：台账 `state/ledger.json`，已换过的自动跳过
- **零依赖**：只要 Python 3.7+ 标准库（bencode 内置）。原生窗口另需 tkinter（Windows/macOS 自带）

---

## 获取

| 方式 | 适合 |
|---|---|
| Docker 镜像 `ghcr.io/littleskyfish/tracker-replace:1.14` | NAS / 常开服务器（下面现成命令） |
| [Releases 下载](../../releases/latest) `tracker-replace-1.14-win64.exe` | **Windows，不用装 Python**，双击就开窗口 |
| 同一个 Release 的 `tracker-replace-1.14-win64-cli.exe` | Windows 上走命令行（doctor/apply，要看输出的） |
| 同一个 Release 的 `tracker-replace-1.14-linux64` | Linux 服务器，`chmod +x` 直接跑 |
| `tracker-replace-1.14-src.zip` 或 `git clone` | macOS / 有 Python 的机器（自己打包用 `build_mac.sh`、`build_windows.bat`） |

---

## 用 Docker 跑（NAS / 服务器常开首选）

```bash
docker run -d --name tracker-replace \
  -v tr-data:/data \
  -p 8642:8642 \
  -e TR_UI_USER=admin \
  -e TR_UI_PASS='自己定一个强密码' \
  --restart unless-stopped \
  ghcr.io/littleskyfish/tracker-replace:1.14
```

浏览器打开 `http://NAS的IP:8642/`，输账号密码登录。不设 `TR_UI_PASS` 则每次启动随机生成（`docker logs tracker-replace` 查看）。仓库自带 `docker-compose.yml`，改密码/端口/路径都有注释。镜像双架构（amd64 / arm64），群晖、绿联、树莓派都能直接拉。

| 挂载 | 作用 |
|---|---|
| `-v tr-data:/data` | 实例配置、种子备份、台账都在这，升级镜像不丢 |
| `-v /宿主机下载目录:/torrents` | 「新种子目录」是宿主机路径时要映射进容器才能填（网页里填 `/torrents`） |
| `-v /旧版工具文件夹:/oldver:ro` | 「一键导入旧版」：打开网页自动认出，路径留空点导入即可 |

安全：监听 0.0.0.0 时网页**强制登录**；在自己电脑上直接跑（127.0.0.1）不弹登录。只放行某台机器：`iptables -I DOCKER-USER -p tcp --dport 8642 ! -s 允许的IP -j DROP`。

---

## 本地跑（Windows / macOS / Linux）

```bash
python3 tracker_replace.py gui   # 原生窗口（没图形环境会提示改用 ui）
python3 tracker_replace.py ui    # 网页窗口，只监听 127.0.0.1，无需登录
```

Windows 双击 `tracker-replace-gui.bat` / `tracker-replace-ui.bat`；macOS 双击对应 `.command`（先 `chmod +x *.command`，第一次右键→打开）。

加实例一栏从上到下：协议（默认「**自动**」：地址端口填完自己探是 http 还是 https，探到 https 且证书自签会顺手勾上"跳过证书校验"并把探测结果写在旁边；也可以手选 http/https。填反了也不要紧，连的时候会自动换过去再试一次，并在日志里告诉你实际是哪个）、类型（留空=自动识别）、地址、端口、用户名、密码。操作顺序：**加实例 → ① 体检 → ② 演练 → 勾"我已确认，可以动真格" → ③ 真执行 → 台账**（不勾确认，③ 只会把确认框闪红提醒，绝不动手）。

命令行等价：

```bash
python3 tracker_replace.py doctor -c config.json --old "旧tracker子串"        # 体检
python3 tracker_replace.py apply  -c config.json --old "..." --torrents-dir ./新种子目录   # 演练(默认不改)
python3 tracker_replace.py apply  -c config.json --old "..." --torrents-dir ./新种子目录 --yes  # 真跑
python3 tracker_replace.py status                                              # 台账概览
```

不想写配置：`--host 127.0.0.1 --port 8080 --user me` + 环境变量 `TRREPLACE_PASSWORD`。

> **从旧版迁移**：点「导入旧版备份/配置」指旧版工具文件夹（或 `import --from <旧目录>`，
> 可先 `--dry-run`），备份/实例/台账一次并进新版；不导也行，`repair`/`rollback`
> 可用 `--pick-state <旧目录>` 每次指过去。

---

## 「跳过校验」支持情况

| 客户端 | 免哈希校验 | 依据 |
|---|---|---|
| qBittorrent ≥ 3.3.13（含 4.x/5.x） | ✅ | `/torrents/add` 的 `skip_checking=true` |
| qBittorrent 5.3.0 | ⚠ 有名 bug 会忽略该参数 | 工具检测到"加入后仍在校验/回流"会撤销新种子 |
| Transmission 全版本 | ❌ | RPC 规范从未暴露 seed_mode / skip_checking |

**Transmission 走带守护的重校验**：备份 `.torrent`+快照 → 删条目（数据不动）→ 暂停态加新 →
启动校验，全程盯 `downloadedEver`——一开始真下载立刻停（绝不重下几十 GB），100% 且 tracker
对才算成功；随时可 `rollback` 按备份原样加回。

**同 hash 的两条路**（新旧 .torrent 是同一份数据时）：

- **原地编辑**（默认，qB 4.1+）：`editTracker` 逐条换，不删不暂停，秒级完成，改完复核，有出入自动回退
- **删除→重加**（老 qB / 原地改失败）：删旧留数据 → `skip_checking` 加新 → 100% 零回流才做种；qB 没收尾时不死等，先干别的稍后自动回看
- hash 对不上 = 站点重做过种子：**明确拒动**，不会拿同名文件硬套

---

## 命令

| 命令 | 作用 |
|---|---|
| `doctor` | 只读体检：型号/版本/能否跳过校验/`--old` 命中多少 |
| `apply` | 换 tracker，**默认演练**，`--yes` 才动手；开工自动找回"有备份却不在列表"的条目（`--no-auto-fix` 关） |
| `status` | 台账概览 + 待重试明细（失败的旧条目都还在） |
| `rollback` | 按备份把某条目加回（不带参数=列全部可回滚；界面有"选回滚种子"不用手填 hash）。加回前先比对现状：一致就不动手；有出入列差异让你选 `--force`/`--keep`。备份在旧版文件夹：`--pick-state <旧目录>` 指一次长期记住 |
| `repair` | 批量一键修复"备份在、条目没了"的种子：暂停+跳过校验认出数据才恢复做种，认不出留暂停只报告 |
| `recover` | 按名字+大小找回丢失条目的数据并加回：`recover --torrents-dir <新种子目录> --root <数据目录>`，只加不删绝不重下，先 `--dry-run` |
| `import` | `import --from <旧版文件夹>` 把旧版备份/实例/台账并进新版（只复制不挪动，同名不覆盖，实例去重） |
| `report` | 指出逐条结果 CSV 位置 |

常用参数：`--old`（**必给**，旧 tracker 匹配子串）、`--torrents-dir`（按 info hash 精确配对，与文件名无关）、`--scheme`（默认 `auto`：自己探这个端口说的是 http 还是 https，探不到按 http；想固定就写 `--scheme https`）/ `--insecure`、`--kind qb|tr`、`--new-url`（不给新种子文件只给 announce）、`--only/--limit`（挑几个试）、`--pause`、`--check-budget`（TR 校验预算，默认 1800s）、`--max-fail`（连败收工，默认 5）。

---

## 安全机制（都在代码里，不靠自觉）

1. 默认演练，`--yes` 才执行
2. 数据文件永不删除；动手前必须成功写出备份，备不上直接拒动
3. qB：新种子确认「100%+做种+零回流」之后才删旧；TR：备份/目录/文件列表任一项取不到就拒动
4. 检测到回流：qB 撤销新种子（旧的没动）；TR 立刻停并记待重试
5. 逐条写 `results.csv` + 日志 + 台账，中断重跑自动跳过已完成
6. 私有种子"不下载"的文件勾选会在新种子上恢复，避免换完补下跳过分卷

---

## 仓库内容

```
tracker_replace.py / tracker_ui.py   # 引擎 + 界面（原生窗口/网页窗口同一引擎）
Dockerfile / docker-compose.yml      # 容器镜像（纯标准库）+ NAS compose 模板
config.example.json                  # 配置模板（无凭据）
build_windows.bat / build_linux.sh / build_mac.sh   # 各平台单文件打包
tracker-replace-gui.bat|.command 等  # 不打包直接双击跑
tests/selftest.py                    # 仿真 qB/TR 靶子 + 170+ 断言（不碰真客户端）
```

exe 只能在 Windows 上打（PyInstaller 不能交叉编译），所以 Release 里的 win64 包由 GitHub Actions 的 windows 机器打；想自己打就在 Windows 上双击 `build_windows.bat`。macOS 同理跑 `build_mac.sh`，被 Gatekeeper 拦就 `xattr -dr com.apple.quarantine dist/tracker-replace`。

运行后多出的 `results.csv`、`state/`、`guiconfig.json`（**含密码**）是你的数据，别提交进仓库。

---

## 排查

| 现象 | 原因 / 处理 |
|---|---|
| TR 返回 409 | CSRF 保护，工具自动取 `X-Transmission-Session-Id` 重发；一直 409 查反代是否吃掉该响应头 |
| TR 加了种子一直 0% | 暂停态不校验（工具已自动 start）。TR 首次校验 status 会报成"下载"，**判回流看 `downloadedEver`** |
| qB 登录后 403 | WebUI 的 CSRF / IP 白名单设置 |
| qB 加入后仍在校验 | `skip_checking` 没生效（5.3.0 已知 bug），工具会撤销并报错 |
| TR 报 `error setting announce list` | 私有种子禁止改 tracker，走删除+重加（本工具默认路径） |
| 换完 pct 不到 100% | 盘上数据本有缺口（旧种子靠 resume 假装 100%），工具已停住：补文件或取消勾选 |
| 网页按钮点了没反应 | 用 1.12+（1.11 内嵌脚本有断行 bug）；或清缓存强刷 |
| 勾了"我已确认"仍提示"这是【真执行】" | 1.13 前网页版 bug：勾选框的 id 撞上浏览器自带的 `confirm`，勾了取不到值（Safari/Chrome 都中招）。1.13 起改用 `#cfm`；被拦时那个框会闪红 |
| 不知道对端是 http 还是 https | 协议留"自动"：填完地址端口自己探，结果写在旁边；选错了也会在连的时候自动换回来并在日志说明 |
| 下载器加错地址后卡很久 | 1.12 起 8 秒内报"连不上"；旧版会重试挂 5 分钟 |

---

## 版本

`-V` 打版本号；窗口标题、网页标签、镜像 tag 同步。

- **1.14** —— 出 Windows exe（Actions 打，没装 Python 的电脑双击就能用）；连不上时把 400/401/403/404 翻译成人话（协议填反 / 前面挡了 Basic 认证 / WebUI 挂在子路径下 / 白名单拦了）。
- **1.13** —— 修网页「我已确认」勾了不生效；协议（http/https）改为自动探测。
- **1.12** —— 新增 Docker 网页版（登录、退出、compose、导入旧版），修网页按钮失灵与连不上时的长时间等待。
- **1.11** —— 改为原地编辑 tracker，不删不暂停，快十几倍，并支持 macOS 打包。
- **1.10** —— 修 qB 删大种子慢时整个批量罢工。
- **1.9** —— 修 Windows 批量报 WinError 10048。
- **1.8** —— 修回滚误伤，TR 校验到 100% 才做种。
- **1.4–1.7** —— 补齐换失败自动回灌、`recover` 找回、开工自动找回、`import` 导入旧版。

验证记录：真实环境数百私有种子/数十 TB 批量通过（qB 4.6.x + TR 4.0.5，零回流）；仿真自检 209 断言全过（含协议探测 12 条、反代报错翻译 11 条、网页窗口真浏览器回归 11 条）；1.12 起 Docker 版另有真浏览器全流程回归（登录/实例增删/导入旧版/退出）。
