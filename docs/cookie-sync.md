# 把浏览器登录态送到 NAS（以及为什么不建议为了签到上 Playwright）

签到脚本唯一脆弱的环节，从来不是"怎么点那个按钮"，而是"凭什么证明你是你"。

绝大多数论坛的登录态就是一段 cookie。这段 cookie 有保质期：普通 Discuz 站点通常
几周，LinuxDo 这类 Discourse 一两周，而挂了瑞数（52pojie）或 Cloudflare 严格模式的，
几小时到一天就要换。所以真正要解决的问题是 —— **让 NAS 上那份 cookie 一直是最新的**，
而不是"让 NAS 自己会登录"。

---

## 一、一键同步：读浏览器现成的 cookie

思路很直接：你每天本来就会用浏览器逛这些站，cookie 一直是最新的。
那就把浏览器里那份现成的，推给 NAS。

### 为什么是读数据库，不是读 `document.cookie`

控制台里敲 `document.cookie` 只能看到 **非 HttpOnly** 的 cookie。
而真正的登录令牌几乎都是 HttpOnly：

| 站点 | 关键 cookie | HttpOnly |
| --- | --- | --- |
| LinuxDo | `_t` | 是 |
| NodeSeek | `session` | 是 |
| 52pojie / 卡饭 / 福利吧 | `xxxx_2132_auth` | 是 |
| V2EX | `A2` | 是 |

从 `document.cookie` 拿到的是一份"看着有、实际登不进去"的残缺 cookie，
这也是很多人复制完还是提示"未登录"的原因。

浏览器的 cookie 数据库里存的是全量，包括 HttpOnly —— 所以脚本直接读数据库。

### 用法

在**你自己的电脑**上跑（不是 NAS 上，NAS 上没有你的登录态）：

```bash
python tools/sync_cookies.py --nas http://192.168.1.10:8181 --remember
```

> 端口用 `docker-compose.yml` 里 `ports:` 冒号左边那个。默认是 `8181:8080`，
> 所以从局域网访问是 **8181**；容器内才是 8080。

第一次会让你输面板口令，之后用 Windows DPAPI 加密存在
`%LOCALAPPDATA%\checkin-sync\panel_secret.bin` —— 只有你这台机器的这个 Windows 账户能解开，
拷到别的机器上没用。

输出大概长这样：

```
来源：Firefox / default-release，共 1843 条 cookie
匹配到 6 个站点：
  KAFAN      5 个字段 213 字节，会话级 cookie
  LINUXDO    3 个字段 148 字节，最早 2026-11-16T10:22:31 过期
  POJIE      6 个字段 342 字节，最早 2026-11-02T23:10:04 过期
  ...
  [OK]   已写入 6 个站点（2026-11-02T20:51:04）
```

### 它具体做了什么

1. 读 `%APPDATA%\Mozilla\Firefox\profiles.ini`，挑默认 profile；找不到就直接扫 `Profiles\` 目录
2. **把 `cookies.sqlite` 连同 `-wal` 复制到临时目录再读** —— Firefox 运行时数据库是锁着的，
   直接连会 `database is locked`；只复制主库又会丢掉 WAL 里还没落盘的最近写入
3. 按域名把 cookie 归到各个站点，丢掉已经过期的
4. 拼成 `name=value; name2=value2`，POST 给面板的 `/api/cookies`
5. 面板写进 `data/cookies.json`，下一轮签到直接生效，**不用重启容器**

### 便携版 / 定制版 Firefox（RunningCheese 之类）

这类版本不是标准安装结构：程序装在 `D:\Program Files (x86)\RunningCheeseFirefox`，
profile 直接放在同目录的 `Profiles\` 下，**没有 `profiles.ini`**，所以自动发现找不到。

第一次手动指定一次，之后会被记住（存在 `%LOCALAPPDATA%\checkin-sync\config.json`）：

```bash
python tools/sync_cookies.py --profile "D:\Program Files (x86)\RunningCheeseFirefox\Profiles" --dry-run
```

> 路径里有空格和括号，**必须加引号**。
> 如果指的是 `cookies.sqlite` 文件本身也能用，脚本会取它所在的目录。

### 一个真实的坑：毫秒时间戳

Firefox 自己的库里 `expiry` 存的是**秒**。但如果这个 profile 曾经从 Chrome / Edge
导入过 cookie（RunningCheese 用户很常见），那些 cookie 的 `expiry` 是**毫秒**。

结果就是：52pojie 的 cookie 算出来的过期时间是「57552 年后」，
`time.localtime()` 直接抛 `OSError: [Errno 22] Invalid argument`，整个同步崩掉。

脚本现在的处理是：超过 `1e11`（17 位量级）的一律当毫秒，除以 1000。
顺带的好处是，那些「看起来还有 5 万年、实际早就过期」的残留 cookie 会被正确剔除
（实测一个库里 POJIE 从 19 个字段降到 15 个，剔掉的都是死数据）。

### 域名映射

| 站点 KEY | 匹配的域名 |
| --- | --- |
| BAIDUWP / TIEBA | `baidu.com`（含 `.baidu.com` 下的 `BDUSS`，两个站共用一份登录态） |
| BILIBILI | `bilibili.com` |
| SMZDM | `smzdm.com` |
| V2EX | `v2ex.com` |
| NODESEEK | `nodeseek.com` |
| LINUXDO | `linux.do` |
| FNNASCLUB | `fnnas.com` |
| WNFLB | `wnflb2023.com` |
| POJIE | `52pojie.cn` |
| KAFAN | `kafan.cn` |

TY189 不在列表里：天翼云盘走账密 + `cloud189-sdk` 登录，和浏览器会话无关。

---

## 二、Edge / Chrome 为什么特殊

Firefox 的 cookie 是**明文** SQLite，随便读。
Edge 和 Chrome 是 AES-GCM 加密，密钥再由 Windows DPAPI 保护一层。

加密格式分两代：

| 前缀 | 含义 | 能不能解 |
| --- | --- | --- |
| `v10` | 传统 DPAPI 保护主密钥 | 能，但要 DPAPI + AES-GCM，得装 `pycryptodome` |
| `v20` | App-Bound Encryption（Chrome 127+ / Edge 同步跟进） | **不能**。密钥由浏览器的提权服务持有，普通进程拿不到 |

v20 要绕开，得往浏览器进程里注入代码（或者拿到 SYSTEM 权限），
复杂度和"直接装个 Playwright"差不多 —— 为一个签到工具不值当。

所以 Edge / Chrome 走这条路：

1. 装 **Cookie-Editor**（或 EditThisCookie）扩展
2. 在目标站点上点扩展 → `Export` → `Export as JSON`
3. `python tools/sync_cookies.py --from-json cookies.json --nas http://192.168.1.10:8181`

导出的 JSON 里有 `domain` 字段，脚本用同一套域名映射归位，效果和读 Firefox 一样。
比手动 F12 复制强的地方是：一次导出整个站点，不用一个个挑。

---

## 三、让它自己跑

cookie 会过期，手动同步迟早会忘。挂在 Windows 计划任务里，每天签到前 20 分钟跑一次：

```cmd
schtasks /Create /TN "checkin-cookie-sync" /SC DAILY /ST 08:50 ^
  /TR "python C:\path\to\checkin-hub\tools\sync_cookies.py --nas http://192.168.1.10:8181"
```

细节：

* 得勾上「只在用户登录时运行」，因为 DPAPI 解密要用你的用户配置，跑在 SYSTEM 下解不开
* 时间点比 `RUN_AT` 早 20 分钟比较稳（默认签到是 09:10）
* 想临时跳过就 `schtasks /Run /TN "checkin-cookie-sync"` 手动触发一次

一个现实的限制：**如果你一整天没开浏览器，cookie 也可能过期**。
好在服务端通常会滑动续期 —— 只要隔天打开一次这些站，有效期就一直往后滚。

---

## 四、那 Playwright / Stagehand 呢

先说结论：**它们解决的不是同一个问题**。

Playwright 能让 NAS "自己登录"，但登录完拿到的 cookie 一样会过期，
过期了还是得再登一次。如果目标只是"每天签到"，那你要的是 cookie 保鲜，
不是浏览器自动化。

### Stagehand 是什么

Stagehand 是 Browserbase 做的开源框架，卖点是"用自然语言操作网页"：
`page.act("点击签到按钮")`，它会截个图/抓个 DOM 丢给大模型，让模型判断该点哪里。

但它不是 Playwright 的轻量替代 —— 官方文档写得很清楚：

* 早期版本直接依赖 Playwright，`npm install` 完还要 `npx playwright install` 拉浏览器
* v4 起换成自研的 CDP + 浏览器扩展内核，**仍然要一个完整的 Chromium**
* **必须配 LLM API Key**（OpenAI / Anthropic），或者用 Browserbase 的 Model Gateway
* 本地跑是免费的，但每次 `act()` 都是一次模型调用，1–5 秒延迟 + 按 token 计费

也就是说：体积比 Playwright 更大，还额外多两条依赖（Node + 大模型）。
对"点一个位置固定的按钮"这种事，用 AI 去猜按钮在哪，属于花更多钱换更不确定的结果。

### 常见方案横向对比

| 方案 | 额外体积 | 能过瑞数 | 适配成本 | 适合这里吗 |
| --- | --- | --- | --- | --- |
| `requests` / `curl_cffi` | ~10 MB | 否 | 已做好 | **默认方案**，12 个站点里 11 个够用 |
| `jsdom` / QuickJS 跑挑战脚本 | ~50 MB | 基本不能 | 极高 | 瑞数会检测 canvas、时序、DOM 特征，脚本每年变 |
| Lightpanda（Zig） | 37 MB | **否** | 中 | 见下 |
| Obscura（Rust） | 40 MB | 大概率不行 | 中 | 见下 |
| Playwright + Firefox | ~300 MB | 能 | 中 | 略轻，NAS 上仍是新 IP + 新指纹，容易二次验证 |
| Playwright + Chromium | ~1 GB | 能 | 中 | 能用，但为一个站点拉 1GB 镜像 |
| Stagehand | 1 GB+ 且要 LLM Key | 能 | 低（自然语言） | 更重、要花钱、有误点风险 |
| **浏览器 cookie 同步** | **0** | **能**（用的是真浏览器换来的凭证） | **极低** | **推荐** |

### 新一代轻量浏览器为什么也不行

2026 年确实出现了一批"从零写的无头浏览器"，比 Chromium 轻一个数量级。
下面是官方基准（Lightpanda 2026-10-19 发布，940 页 / 25 并发）：

| 项目 | 语言 | 峰值内存 | Web 标准通过率 | 协议 | 许可 |
| --- | --- | --- | --- | --- | --- |
| Lightpanda | Zig | 37 MB | 80% | CDP / WebDriver / MCP | AGPL-3.0 |
| Obscura | Rust | 40 MB | 30% | CDP / MCP | Apache-2.0 |
| Moli | Rust | 92 MB | 未跑完 | CDP / WebDriver | Apache-2.0 / MIT |
| Headless Chrome | C++ | 207 MB | 100% | CDP | — |

数字很香，但**过不了瑞数**，原因很具体：

* Lightpanda 为了快，**把 CSS / GPU 渲染层整个删掉了**，官方自己的定位就写着
  "视觉渲染 ❌、反爬能力弱、容易暴露无头与无渲染特征"。瑞数会读
  `canvas.getContext('2d')` 的像素结果、`getBoundingClientRect()` 的尺寸、WebGL 参数 ——
  在一个没有渲染引擎、所有盒子都是 0×0 的环境里，这些值全是空的，
  脚本当场判定爬虫，连挑战都不给你出。
* Obscura 支持渲染，但 Web Platform Tests 只过 **30%**（Lightpanda 80%、Chrome 100%），
  canvas / WebGL 这类冷门 API 大概率缺失，同样过不了。
* 真想对抗指纹检测只有 **Camoufox**（C++ 魔改 Firefox，硬件级伪装），
  但它的内存和常规浏览器一个量级，一点也不轻。

所以这条路是死结：**越轻量的浏览器，渲染和指纹越假；而瑞数检测的恰好就是这些。**

（顺带纠正一个印象：Rust 在这类项目里确实很流行 —— Obscura、Moli、Cloudflare Kitesurf
都是 Rust。但名气最大的 Lightpanda 其实是 **Zig** 写的，只是内部用了 Rust 的
`html5ever` 做 HTML 解析。）

### 瑞数（WZWS）为什么绕不过去

52pojie 挂的不是 Cloudflare，是瑞数。两者拦人的方式完全不同：

* **Cloudflare** 看 TLS 握手指纹（JA3/JA4）+ HTTP/2 帧特征 → 换 `curl_cffi` 的
  `impersonate="chrome"` 就能过，这也是 NodeSeek、LinuxDo 现在的做法
* **瑞数** 让你先执行一段高度混淆的 JS，算出一次性参数，再拿去换短期 cookie。
  它不看你的 TLS 长什么样，看你**算不算得出来**

前者是"你长得像不像浏览器"，后者是"你会不会做这道题"。所以纯 HTTP 客户端对它没辙，
GitHub 上那几个 52pojie 签到脚本（包括 `jwmycz/py52pojie` 的 `waf.js`）现在全部失效，
原因都是瑞数换了挑战算法、依赖的参数已经不存在了。

真要每天稳定过瑞数，只有两条路：跑真浏览器，或者用真浏览器换来的 cookie。
后者的成本是零。

---

## 五、安全检查清单

* `data/cookies.json` 是**明文**凭据，权限 600，和 `.env` 同级对待；**不要提交到 git**
  （`.gitignore` 已经覆盖 `data/`）
* 同步走的是 HTTP 明文，所以**只在局域网内跑**。面板别直接暴露到公网；
  要从外网访问就套一层 HTTPS 反代（顺便把 `PANEL_COOKIE_SECURE=true` 打开）
* 脚本用 `Authorization: Bearer <面板口令>` 鉴权，并且**拒绝任何带 `Origin` 头的请求** ——
  浏览器发起的跨站请求一定带 `Origin`，命令行脚本不带，挡掉网页伪造这条路
* 口令用 DPAPI 存，只有当前 Windows 账户能解；换机器、换用户都要重新输

---

## 六、常见问题

**Q：同步了还是提示 cookie 失效？**

先看日志里是哪一类错误。本项目的错误分类是分开的：

* `Cookie 已失效 / 未登录` → 真的过期了，重新同步
* `被瑞数挡了` / `被 Cloudflare 拦下来了` → 请求没到站点，是 IP 或指纹问题，换 cookie 没用
* `无法连接` / `超时` → 网络问题，跟 cookie 无关

**Q：一个站有多个账号怎么办？**

同步只覆盖**第一个**账号的 cookie。第二个账号仍然用 `_2` 后缀写在 `.env` 里
（`POJIE_COOKIE_2=...`），或者干脆用第二个浏览器 profile 跑一次 `--profile` 指定。

**Q：不想在电脑上装 Python？**

可以。面板「设置」页 → 浏览器 Cookie 同步 → 展开"手动粘贴"，
选站点、把 F12 里那行 Cookie 粘进去、点导入。脚本只是把这一步自动化了而已。

**Q：同步完要重启容器吗？**

不用。面板把 cookie 写进 `data/cookies.json`，`main.py` 每轮开始时重新读配置。

**Q：这个方案对 GitHub Actions 有用吗？**

没有。Actions 跑在云端、用的是机房 IP，你的浏览器 cookie 拿过去大概率直接被判异常。
Actions 那条路还是老实填 Secrets —— 反正一天只跑一次，手动更新频率可以接受。
