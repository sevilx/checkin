# checkin

多站点自动签到工具。两种用法，任选其一：

- **GitHub Actions**：不用服务器，Fork 一份、填几个 Secret，每天定时跑完给你发通知
- **NAS / Docker**：容器里常驻，带网页面板，能看每个站点的结果和历史，还能点一下手动签到

支持 9 个站点，只依赖 `requests` 一个第三方库，浏览器面板用 Python 标准库实现，不装任何 Web 框架。

---

## 支持的站点

| 站点 | 凭据 | 做了什么 |
| --- | --- | --- |
| 百度网盘 | Cookie（`BDUSS`、`STOKEN`） | 会员成长值签到 + 每日答题 |
| 哔哩哔哩 | Cookie（`SESSDATA`、`bili_jct`） | 直播签到 + 漫画签到 + 大会员权益 |
| 飞牛 NAS 论坛 | Cookie（`pvRK_2132_auth`） | Discuz 每日打卡 |
| 什么值得买 | Cookie（`sess`） | APP 接口签到 + 奖励明细 |
| 百度贴吧 | Cookie（`BDUSS`） | 按关注列表逐吧签到 |
| V2EX | Cookie 或 `A2` | `/mission/daily` 领每日登录奖励 |
| NodeSeek | Cookie（`session`） | 随机 / 固定鸡腿 |
| 天翼云盘 189 | 手机号 + 密码 | 个人空间签到 + 容量变化 |
| LinuxDo | Cookie | 登录保活 + 浏览任务（该站本身没有签到按钮） |

同时具备：

- **多账号**：同一个站点可以配无限个账号，结果分开显示
- **站点级代理**：每个站点可以走不同的代理，也可以设全局兜底
- **推送通知**：Telegram、企业微信、飞书、Server酱³、PushPlus、Bark、Gotify、ntfy，填哪个用哪个
- **网页面板**（Docker 模式）：最近一次结果、最近 30 次历史、一键触发
- **日志脱敏**：cookie 和密码在输出里统一变成 `abcd***wxyz`
- **单站点隔离**：某个站点挂了不影响其他站点，也不会中断整轮

---

## 快速开始

三种方式选一种就行，不要同时开两套（同一账号一天会被签两次，虽然多数站点幂等，但没必要）。

| 方式 | 需要什么 | 适合谁 |
| --- | --- | --- |
| [GitHub Actions](#方式一github-actions不用服务器) | 一个 GitHub 账号 | 没有服务器，只想每天收个通知 |
| [NAS / Docker](#方式二nas--docker带网页面板) | 能跑容器的 NAS 或小主机 | 想随时打开网页看签到状态 |
| [本机直接跑](#方式三本机直接跑) | Python 3.10+ | 调试、试跑、临时用 |

---

### 方式一：GitHub Actions（不用服务器）

#### 1. Fork 本仓库

点右上角 **Fork** → **Create fork**。你自己的那份仓库是私有的还是公开的都行，凭据不会进代码。

#### 2. 启用 Actions

Fork 出来的仓库默认不跑工作流。进 **Actions** 标签，看到提示就点 **I understand my workflows, go ahead and enable them**，左侧列表里出现 `checkin` 就算好了。

#### 3. 加 Secrets

`Settings → Secrets and variables → Actions → New repository secret`，只添加你真正拥有的站点，没有的留空不填即可：

| Secret 名 | 值 |
| --- | --- |
| `BAIDUWP_COOKIE` | 百度网盘的 Cookie（含 `BDUSS`、`STOKEN`） |
| `BILIBILI_COOKIE` | B 站 Cookie（含 `SESSDATA`、`bili_jct`） |
| `FNNASCLUB_COOKIE` | 飞牛论坛 Cookie（含 `pvRK_2132_auth`） |
| `SMZDM_COOKIE` | 什么值得买 Cookie（`sess` 不能少） |
| `TIEBA_COOKIE` | 贴吧 Cookie（含 `BDUSS`） |
| `V2EX_COOKIE` 或 `V2EX_A2` | 二选一，`A2` 够用 |
| `NODESEEK_COOKIE` | 至少含 `session`，建议连 `pjwt` 一起 |
| `NODESEEK_METHOD` | `random`（默认）或 `fixed`，可不填 |
| `LINUXDO_COOKIE` | 含 `_t`、`_forum_session` |
| `TY189_USERNAME` / `TY189_PASSWORD` | 天翼账号 + 密码 |
| 推送渠道 | 见[推送通知](#推送通知)，至少配一组，不配就只写日志 |

Cookie 怎么取见[凭据怎么填](#凭据怎么填)。

**多账号**：在变量名后面加 `_1`、`_2`，例如 `V2EX_COOKIE_1`、`V2EX_COOKIE_2`，数量不限。

**只跑部分站点**：`Settings → Secrets and variables → Actions → Variables` 标签页里新建 `ENABLED_SITES`，值写小写 KEY，逗号分隔，例如 `v2ex,nodeseek`。

#### 4. 手动跑一次

`Actions → 左侧 checkin → Run workflow`。想先只测一个站，在输入框里填 `v2ex`。

跑完看两个地方：**Job 日志**（逐站点明细）和这次运行页面的 **Summary**（一张结果表）。

#### 5. 改执行时间（可选）

默认每天北京时间 09:10。改 `.github/workflows/checkin.yml`：

```yaml
on:
  schedule:
    - cron: "10 1 * * *"      # UTC 时间，= 北京时间 09:10
```

**GitHub 的 cron 用 UTC，北京时间要减 8 小时**：

| 想在北京时间 | 写成 |
| --- | --- |
| 00:05 | `5 16 * * *` |
| 08:00 | `0 0 * * *` |
| 09:10 | `10 1 * * *` |
| 12:30 | `30 4 * * *` |

> 注意两个坑：
> ① 仓库**连续 60 天没有任何提交**，GitHub 会自动停用定时任务，进 Actions 手动跑一次即可恢复；
> ② 定时不精确，高峰期会延迟 5~30 分钟，别设在 23:59 这种卡点的时刻。

---

### 方式二：NAS / Docker（带网页面板）

镜像里同时装了 Python 和 Node（189 需要 Node）。

```bash
git clone https://github.com/<你的用户名>/checkin.git
cd checkin
cp .env.example .env
vi .env                          # 填站点凭据、PANEL_PASSWORD、推送渠道
docker compose up -d --build
```

然后打开 `http://NAS的IP:8181`，第一次会让你设口令：

- `.env` 里写了 `PANEL_PASSWORD=xxx` → 用这个
- 没写 → 首次启动随机生成，打印在 `docker logs checkin` 里（只显示一次）
- 忘了 → 改 `PANEL_PASSWORD` 后 `docker compose up -d` 重启

`docker-compose.yml` 里可以调的开关：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `RUN_AT` | `09:10` | 每天几点跑（容器时区） |
| `RUN_ON_START` | `false` | 启动后先跑一次 |
| `PANEL_ENABLED` | `true` | 是否起网页面板 |
| `PANEL_ALLOW_RUN` | `true` | 是否允许在网页上点"立即签到" |
| `PANEL_PASSWORD` | 空 | 面板口令，建议写在 `.env` 里 |

**不想用容器内置定时**（比如 NAS 自带的任务计划更可靠）：把 `RUN_AT` 留空，容器只跑一次就退出，由外部调度：

```bash
docker compose run --rm -e RUN_AT= checkin
```

**数据都在 `./data`**：`status.json`（最近一次）、`runs.json`（最近 30 次）、`last-run.log`、`panel_auth.json`。备份这个目录就是完整快照，删掉等于清空历史。

**升级**：

```bash
git pull
docker compose up -d --build
```

---

### 方式三：本机直接跑

```bash
pip install -r requirements.txt
cd runtime189 && npm ci && cd ..     # 只有 189 需要；Windows 下别用 npm --prefix
cp .env.example .env                 # 填凭据
python main.py --list                # 看站点清单和要配的变量
python main.py --no-notify           # 空跑，只看日志不推送
python main.py                       # 正式跑 + 推送
python -m panel.server               # 起面板，默认 http://127.0.0.1:8080
```

没装 Node 时 189 会被自动跳过并提示，其余站点照常执行。

---

## 凭据怎么填

### Cookie 通用取法

1. 浏览器登录目标站点（**建议用无痕窗口或专门的浏览器**，避免和日常使用混在一起）
2. 按 `F12` → **Application（应用）** → 左侧 **Cookies** → 选中该站点的域名
3. `Ctrl+A` 全选 → `Ctrl+C` 复制，粘贴成 `key=value; key2=value2` 的形式
4. 有些站点（B 站、什么值得买）必须整段复制，少一个关键字段就会失败

也可以用插件（如 Cookie-Editor）一键导出，格式选 `Header String` 或 `Netscape` 里的 header 形式。

Cookie 相当于账号密码，**别发给任何人、别贴到 issue 里**。失效是这类工具的通病，日志会明确写"Cookie 已失效"，重新复制一次即可。

### 两种配置来源

**A. 环境变量**（推荐，也是 Actions 唯一的方式）

写在 `.env` 里，或者配成 GitHub Secrets。变量名见上面的表格，`.env.example` 里有全量清单。

**B. `config.json`**（适合账号多、想一次批量管理）

```bash
cp config.example.json config.json
```

```json
{
  "V2EX": [{ "cookie": "A2=xxx", "name": "主号" }],
  "TY189": [{ "username": "13800000000", "password": "xxx", "name": "小号" }],
  "NODESEEK": [{ "cookie": "session=xxx", "method": "fixed" }]
}
```

- KEY 与 [dailycheckin-plus](https://github.com/BeacherZ/dailycheckin-plus) 一致，从那边迁过来可以只改格式
- 一个站点写数组就是多账号，`name` 只是显示用的备注
- 数组里每一项都能加 `"proxy": "http://192.168.1.2:7890"`
- 环境变量优先于 `config.json`；两者都配了，用环境变量的
- `config.json` 已被 `.gitignore` 忽略，Docker 部署时在 `docker-compose.yml` 里取消那行注释即可挂载进容器

---

## 代理

三层，就近生效：

```bash
# 1. 站点级：只影响这一个站点
V2EX_PROXY=http://192.168.1.2:7890
NODESEEK_PROXY=socks5h://192.168.1.2:7891

# 2. 全局兜底：账号没单独配时用它
GLOBAL_PROXY=http://192.168.1.2:7890
```

```json
// 3. config.json 里按账号配（优先级最高）
"V2EX": [{ "cookie": "...", "proxy": "http://192.168.1.2:7890" }]
```

支持 `http://`、`https://`、`socks5://`、`socks5h://`（socks 需要额外装 `requests[socks]`）。

**189 是例外**：它走 `cloud189-sdk` 登录，代理设置对它不生效。它是国内站点，直连即可；配了代理会在结果里提示一句，不会假装成功。

典型场景：机器在国内，V2EX / NodeSeek / LinuxDo 时好时坏 → 给这三个站点配代理，其余直连。

---

## 推送通知

填了哪个走哪个，全不填就只打印日志。

| 渠道 | 变量 |
| --- | --- |
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`（先给 bot 发一条消息，否则拿不到 chat id） |
| 企业微信群机器人 | `WECOM_WEBHOOK` |
| 飞书群机器人 | `FEISHU_WEBHOOK` |
| Server酱³ | `SC3_PUSH_KEY` |
| PushPlus | `PUSHPLUS_TOKEN` |
| Bark | `BARK_URL`（`https://api.day.app/你的key`） |
| Gotify | `GOTIFY_URL` + `GOTIFY_TOKEN` |
| ntfy | `NTFY_TOPIC`（自建服务再改 `NTFY_URL`） |

> 通知内容带账号昵称和余额，别推到公开群；PushPlus、Server酱这类第三方服务等于把这些信息交给对方，介意的话用 Telegram 或自建 ntfy。

---

## 网页面板

只在 Docker 模式下有意义。面板能看到：每个账号最近一次的结果、最近 30 次历史、一键触发签到。

**没有远程改 cookie 的功能** —— 凭据只存在 `.env` / `config.json` 里，网页改不了，这是有意的，减少暴露面。

界面是自适应深浅色的卡片布局：每个账号一张卡，绿 / 红状态条，下面列明细；顶部是"成功 / 总数、上次耗时、最近更新、需要处理"，下面是历史表格。每 30 秒自动刷新，任务在跑时顶部有提示条。

想先看长什么样：浏览器打开 [`docs/preview.html`](docs/preview.html)，静态假数据，不用起服务。

**安全默认**（细节见 [SECURITY.md](SECURITY.md)）：

- 必须登录才能看到任何数据，未登录访问页面直接跳登录页
- 口令只存 PBKDF2 派生值，不存明文；改口令会同时让所有旧会话失效
- 会话是 HMAC 签名票据，不是把口令塞进 cookie
- 登录失败限速 6 次 / 5 分钟
- 页面不加载任何外部资源，CSP `default-src 'self'`，禁止被 iframe 嵌套
- 不记录访问日志，避免 URL 和参数进容器日志

> 只在局域网用就不要往公网映射。要公网访问，前面必须套一层 HTTPS 反代（Caddy / Nginx / 群晖反代），并把 `PANEL_COOKIE_SECURE` 设为 `true`。不要裸奔。

---

## 隐私与安全

**核心承诺：cookie / 密码不落盘、不进日志、不进 git。**

| 数据 | 去哪了 |
| --- | --- |
| 站点凭据 | 只在运行内存和 `.env` / Secrets 里；不写入 `data/`，不打印 |
| 运行日志 | 凭据统一替换成 `abcd***wxyz` |
| `data/status.json` | 只存展示用的文字：站点名、备注、结果、耗时 |
| GitHub Actions 日志 | 只有签到结果文字；公开仓库的日志任何人都能看，介意的话跑完在运行页面右上角 `...` → **Delete workflow run** |

- Actions 只申请 `contents: read` 权限，不上传 artifact
- 依赖版本用 `package-lock.json` 锁定，配了 Dependabot 每周检查
- `.gitignore` 已忽略 `.env`、`config.json`、`data/`；`.dockerignore` 保证凭据不会被打进镜像

完整的数据流向表、凭据泄漏处置步骤见 **[SECURITY.md](SECURITY.md)**。发现安全问题请用仓库 Security 标签页的 **Report a vulnerability** 私下报告，不要在 issue 里贴 cookie。

---

## FAQ

**Cookie 又失效了？**
这类工具的通病，日志会写"Cookie 已失效（被重定向到登录页）"，重新复制一次即可。NodeSeek 建议连 `pjwt` 一起复制，能显示用户名。

**189 报"设备ID不存在"或要二次验证？**
云盘在陌生 IP（GitHub 服务器、容器新出口）登录会触发设备校验。先在手机或网页端登录一次并信任该设备，或临时关闭设备锁再跑。

**贴吧很慢？**
关注列表要分页拉，每吧间隔 1~2 秒防风控，几十个吧跑 2~3 分钟正常，工作流的 40 分钟超时够用。

**面板打不开？**
看 `docker logs checkin` 里有没有 `[panel] 监听 http://...`；确认 NAS 防火墙和端口映射放通 8181；`./data` 必须挂载，否则重启状态就没了。

**忘了面板口令？**
改 `.env` 里的 `PANEL_PASSWORD`，然后 `docker compose up -d`。旧口令立即失效。

**LinuxDo 为什么没有"签到成功"？**
linux.do 是 Discourse 论坛，本身没有签到功能。社区脚本做的都是登录保活 + 浏览主题，本项目同样只做这两件事。

**Actions 跑成功了但没收到通知？**
先确认配的是哪一组变量、两个变量是否都填了（Telegram 需要 token 和 chat id 同时存在）；再点开日志看末尾有没有推送报错。

---

## 致谢

签到协议与思路参考了这些开源项目，向原作者致谢：

- [Sitoi/dailycheckin](https://github.com/Sitoi/dailycheckin) — MIT
- [BeacherZ/dailycheckin-plus](https://github.com/BeacherZ/dailycheckin-plus) — MIT
- [Chankin026/linuxdo-v2ex-checkin](https://github.com/Chankin026/linuxdo-v2ex-checkin)
- [wes-lin/Cloud189Checkin](https://github.com/wes-lin/Cloud189Checkin) — ISC，运行时依赖 `cloud189-sdk`

想加站点或改代码，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 免责

仅用于个人账号的自动化，请勿批量养号、多线程刷站。使用自动化脚本存在账号被风控的风险，请自行评估。凭据只存在你自己的 Secrets 或 NAS 上，本项目不上传、不收集任何数据。

[MIT License](LICENSE) © 2026 Ben
