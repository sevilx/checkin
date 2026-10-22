# checkin

多站点自动签到，两种部署方式：**GitHub Actions 定时跑**，或**家里 NAS 上用 Docker 常驻 + 网页面板看状态**。

- 9 个站点：百度网盘、哔哩哔哩、飞牛 NAS 论坛、什么值得买、百度贴吧、V2EX、NodeSeek、天翼云盘 189、LinuxDo
- 每个站点可单独配代理（`V2EX_PROXY=...`），也可设全局 `GLOBAL_PROXY`
- 自带网页面板：看最近一次结果、历史记录，能一键触发签到
- 只依赖 `requests` 一个第三方库，面板用标准库 `http.server`，零 Web 框架
- 189 用一个 Node 小工具桥接 `cloud189-sdk`（协议要 RSA 加密 + 设备指纹，不值得重写）
- 凭据不落盘、不进日志、不进 git

| 站点 | 配置 KEY | 凭据 | 做了什么 |
| --- | --- | --- | --- |
| 百度网盘 | `BAIDUWP` | Cookie（BDUSS、STOKEN） | 会员成长值签到 + 每日答题 |
| 哔哩哔哩 | `BILIBILI` | Cookie（SESSDATA、bili_jct） | 直播签到 + 漫画签到 + 大会员权益 |
| 飞牛 NAS 论坛 | `FNNASCLUB` | Cookie（pvRK_2132_auth） | Discuz 打卡 |
| 什么值得买 | `SMZDM` | Cookie（sess） | APP 接口签到 + 奖励明细 |
| 百度贴吧 | `TIEBA` | Cookie（BDUSS） | 按关注列表逐吧签到 |
| V2EX | `V2EX` | Cookie 或 `A2` | `/mission/daily` 领每日登录奖励 |
| NodeSeek | `NODESEEK` | Cookie（session） | 随机 / 固定鸡腿 |
| 天翼云盘 189 | `TY189` | 手机号 + 密码 | 个人空间签到 + 容量变化 |
| LinuxDo | `LINUXDO` | Cookie | 登录校验 + 浏览任务（该站没有签到按钮） |

签到协议参考了 [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin)、[BeacherZ/dailycheckin-plus](https://github.com/BeacherZ/dailycheckin-plus)、[Chankin026/linuxdo-v2ex-checkin](https://github.com/Chankin026/linuxdo-v2ex-checkin)、[wes-lin/Cloud189Checkin](https://github.com/wes-lin/Cloud189Checkin)，均为开源实现，向原作者致谢。

---

## 一、为什么不直接改 dailycheckin-plus

dailycheckin-plus 的 `config.json` 只是**数据**，每个站点是镜像里固定的 Python 模块（`dailycheckin_plus/<站点>/main.py`），引擎按硬编码列表加载，没有"读配置就多一个站点"的插件机制。所以：

* NodeSeek **它已内置**（KEY = `NODESEEK`），填 cookie 就行；
* LinuxDo / 189 **不行**：前者依赖浏览器自动化（DrissionPage），后者依赖 Node + `cloud189-sdk`，而且 189 没有可用的 Cookie 接口，只认账号密码登录。

本项目是独立实现，**config.json 的 KEY 与 dailycheckin-plus 完全一致**，NAS 上那份 `config/config.json` 可以直接复制过来。

---

## 二、部署到 GitHub（定时跑）

### 1. 建仓库

GitHub → **New repository** → Repository name 填 `checkin` → 可见性选 **Public**（仓库里没有任何凭据，cookie 全部走 Secrets）→ **Add .gitignore / Add license 都别勾**（项目里已自带这两份文件）→ Create repository。

### 2. 传代码（网页版）

空仓库页面点 `…or upload an existing file` → 打开本地项目目录，**Ctrl+A 全选里面的内容**拖进浏览器 → Commit changes。

- 拖**内容**，不要拖文件夹本身，否则仓库里会多套一层目录，Actions 找不到 `main.py`
- `.github/`、`.gitignore`、`.dockerignore`、`.gitattributes`、`.env.example` 必须一起传上去；传完回仓库首页确认根目录有 `.github`
- 网页上传**不读 `.gitignore`**，传之前把 `.env`、`config.json`、`data/` 清掉（`__pycache__` 也是）
- 漏了 `.github`：`Add file → Create new file`，文件名直接输入 `.github/workflows/checkin.yml`，粘贴内容后提交

> 习惯命令行的等价做法：`git init && git add . && git commit -m "init checkin" && git branch -M main && git remote add origin https://github.com/<你的用户名>/checkin.git && git push -u origin main`。`.gitignore` 已忽略 `.env` / `config.json` / `data/`，别用 `git add -f` 强行塞进去。

### 3. 加 Secrets

`Settings → Secrets and variables → Actions → New repository secret`，只加你实际有的：

| Secret | 说明 |
| --- | --- |
| `BAIDUWP_COOKIE` | 含 `BDUSS`、`STOKEN` |
| `BILIBILI_COOKIE` | 含 `SESSDATA`、`bili_jct` |
| `FNNASCLUB_COOKIE` | 含 `pvRK_2132_auth` |
| `SMZDM_COOKIE` | 含 `sess` |
| `TIEBA_COOKIE` | 含 `BDUSS` |
| `V2EX_COOKIE` 或 `V2EX_A2` | 二选一 |
| `NODESEEK_COOKIE` | 至少含 `session`，建议连 `pjwt` 一起 |
| `NODESEEK_METHOD` | `random`（默认）/ `fixed` |
| `LINUXDO_COOKIE` | 含 `_t`、`_forum_session` |
| `TY189_USERNAME` / `TY189_PASSWORD` | 天翼账号 + 密码 |
| 推送 | 见第五节，至少配一个 |

**多账号**：变量名后加 `_1`、`_2`，如 `V2EX_COOKIE_1`、`V2EX_COOKIE_2`。

**只跑部分站点**：`Settings → Secrets and variables → Actions → Variables` 新建 `ENABLED_SITES = v2ex,nodeseek`（小写 KEY，逗号分隔）。

### 4. 手动跑一次

`Actions → checkin → Run workflow`。日志会逐站点打印，页面 Summary 有一张结果表。

### 5. 改定时

```yaml
on:
  schedule:
    - cron: "10 1 * * *"      # UTC，= 北京 09:10
```

**GitHub 用 UTC，北京时间减 8 小时**：09:10 → `10 1 * * *`；08:00 → `0 0 * * *`；12:30 → `30 4 * * *`。

> **两个坑**：① 仓库 60 天没有任何提交，schedule 会被自动停用；② 高峰期会延迟 5~30 分钟，别卡在 23:59。

---

## 三、网页面板（NAS / 本地）

面板做的事：看最近一次每个站点的结果、最近 30 次历史、点"立即签到"手动跑一次。**没有远程改 cookie 的功能** —— 凭据只在 `.env` / `config.json` 里，网页改不了，减少暴露面。

启动后打开 `http://NAS的IP:8181`，第一次会要求口令：

- `.env` 里写了 `PANEL_PASSWORD=xxx` → 用这个
- 没写 → 首次启动随机生成，打印在 `docker logs checkin` 里（只显示一次）
- 忘了 → 改 `PANEL_PASSWORD` 后 `docker compose up -d` 重启

界面是自适应深浅色的卡片布局：每个账号一张卡，绿/红状态条，下面列结果明细；顶部是"成功 / 总数、上次耗时、最近更新、需要处理"，下面是历史表格。页面每 30 秒自动刷新，任务在跑时顶部有提示条。

> 想先看长什么样：用浏览器打开 [`docs/preview.html`](docs/preview.html)（静态假数据，不需要起服务）。

**安全默认**（详见 [SECURITY.md](SECURITY.md)）：必须登录才能看到任何数据、口令只存 PBKDF2 派生值、会话是 HMAC 签名票据、登录失败限速、页面不加载任何外部资源、CSP `default-src 'self'`。

> 只在局域网用就不要往公网映射。要公网访问，前面必须套 HTTPS 反代（Caddy / Nginx / 群晖反代），别裸奔。

---

## 四、代理

三个层级，就近生效：

```bash
# 1. 站点级：只影响这个站点
V2EX_PROXY=http://192.168.1.2:7890
NODESEEK_PROXY=socks5h://192.168.1.2:7891

# 2. 全局兜底：账号没单独配时用它
GLOBAL_PROXY=http://192.168.1.2:7890
```

```json
// 3. config.json 里按账号配（优先级最高）
"V2EX": [{ "cookie": "...", "proxy": "http://192.168.1.2:7890" }]
```

支持 `http://`、`https://`、`socks5://`、`socks5h://`（socks 需要装 `requests[socks]`）。

**189 是例外**：它走 `cloud189-sdk` 登录，代理设置对它不生效。它是国内站点，直连即可；配了代理会在结果里提示一句。

典型用法：NAS 在国内，V2EX / NodeSeek / LinuxDo 时好时坏 → 给这三个站点配楼梯，其余直连。

---

## 五、推送通知

填了哪个走哪个，全不填就只打印日志。

| 渠道 | 变量 |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`（先给 bot 发一条消息） |
| 企业微信群机器人 | `WECOM_WEBHOOK` |
| 飞书群机器人 | `FEISHU_WEBHOOK` |
| Server酱³ | `SC3_PUSH_KEY` |
| PushPlus | `PUSHPLUS_TOKEN` |
| Bark | `BARK_URL`（`https://api.day.app/你的key`） |
| Gotify | `GOTIFY_URL` + `GOTIFY_TOKEN` |
| ntfy | `NTFY_TOPIC`（可选 `NTFY_URL`） |

---

## 六、NAS 上用 Docker Compose

```bash
cd checkin
cp .env.example .env
vi .env                          # 填站点凭据、PANEL_PASSWORD、推送渠道
docker compose up -d --build
docker logs -f checkin       # 首次启动会打印面板口令（如果没设 PANEL_PASSWORD）
```

打开 `http://NAS的IP:8181`。

`docker-compose.yml` 里几个开关：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `RUN_AT` | `09:10` | 每天几点跑（容器时区）；**留空=单次执行后退出** |
| `RUN_ON_START` | `false` | 启动后先跑一次 |
| `PANEL_ENABLED` | `true` | 是否起网页面板 |
| `PANEL_ALLOW_RUN` | `true` | 是否允许网页上点"立即签到" |
| `PANEL_PASSWORD` | 空 | 面板口令 |

外部 cron 调度（不用容器内置定时）：

```bash
docker compose run --rm -e RUN_AT= checkin
```

数据在 `./data`：`status.json`（最近一次）、`runs.json`（最近 30 次）、`last-run.log`、`panel_auth.json`。整个目录备份走就是完整快照。

---

## 七、本机直接跑

```bash
pip install -r requirements.txt
cd runtime189 && npm ci && cd ..     # 只有 189 需要；Windows 下别用 npm --prefix
cp .env.example .env                 # 填凭据
python main.py --list                # 看站点和要配的变量
python main.py --no-notify           # 空跑看日志
python main.py                       # 正式跑 + 推送
python -m panel.server               # 起面板（默认 8080）
```

没装 Node 时 189 会被跳过并提示，其余站点照常执行。

---

## 八、隐私与安全

**核心承诺：cookie / 密码不落盘、不进日志、不进 git。**

- 运行日志里统一替换成 `abcd***wxyz`
- `data/status.json` 只存展示用的文字（站点名、备注、结果），不存凭据
- 面板必须登录；口令只存 PBKDF2 派生值
- Actions 只申请 `contents: read`，不上传 artifact
- 通知会带账号昵称和余额，别把通知转发到公开群

完整的数据流向表、开源前检查清单、凭据泄漏处理步骤见 **[SECURITY.md](SECURITY.md)**。

> ⚠️ 如果你是从 dailycheckin-plus 迁过来的：**那个项目的 `config/config.json` 里有明文 cookie**，别把整个目录一起推到公开仓库。只把内容复制到 `.env` 或本项目被 gitignore 的 `config.json`。

---

## 九、目录结构

```
checkin/
├─ main.py                  # 入口：读配置 → 跑站点 → 汇总 → 写状态 → 推送
├─ checkin/
│  ├─ core.py               # Site 基类、cookie 解析、重试、日志脱敏
│  ├─ config.py             # 环境变量 / config.json 两套配置源
│  ├─ notify.py             # 8 个推送渠道
│  ├─ store.py              # data/status.json、runs.json
│  └─ sites/                # 一个站点一个文件
├─ panel/
│  ├─ server.py             # 标准库 HTTP 服务 + 登录鉴权
│  ├─ index.html            # 状态页（无外部资源）
│  └─ login.html
├─ runtime189/              # 189 的 Node 桥接（cloud189-sdk）
├─ .github/workflows/       # GitHub Actions 定时任务
├─ Dockerfile               # Python + Node 双运行时
└─ docker-compose.yml
```

---

## 十、加一个站点

1. `checkin/sites/xxx.py`：继承 `Site`，填 `key` / `name` / `fields` / `required`，实现 `run() -> list[Line]`
2. `checkin/sites/__init__.py` 的 `SITES` 加一行
3. 需要凭据的话，在 workflow 的 `env:` 里加对应 Secret
4. `python main.py --list` 确认能识别

`run()` 正常返回＝成功，抛异常＝失败。主控会捕获、汇总、推送，不会因为一个站点崩掉整轮。代理、多账号、面板统计都是自动支持的。

---

## 十一、FAQ

**Cookie 又失效了？**
这类工具的通病。日志会写"Cookie 已失效"，重新复制一次即可。NodeSeek 建议连 `pjwt` 一起复制（能显示用户名）。

**189 报"设备ID不存在"或要二次验证？**
云盘在陌生 IP（GitHub 服务器、容器新出口）登录会触发设备校验。先在手机/网页端登录一次并信任该设备，或临时关闭设备锁再跑。

**贴吧很慢？**
关注列表要分页拉，每吧间隔 1~2 秒防风控，几十个吧跑 2~3 分钟正常，`timeout-minutes: 40` 够用。

**面板打不开？**
看 `docker logs checkin` 里面板有没有起来；NAS 防火墙 / 端口映射确认 8181 通；`./data` 必须挂载，否则重启状态就没了。

**LinuxDo 为什么没有"签到成功"？**
linux.do 是 Discourse 论坛，本身没有签到功能。社区脚本做的都是登录保活 + 浏览主题，本项目同样只做这两件事。

---

## 十二、免责

仅用于个人账号的自动化，请勿批量养号、多线程刷站。使用第三方自动化脚本存在账号被风控的风险，自行评估。凭据只存在你自己的 GitHub Secrets 或 NAS 上。

MIT License，见 [LICENSE](LICENSE)。
