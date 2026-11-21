# 吾爱破解瑞数（WZWS）真浏览器 PoC

> ## 🔴 先看这条：高风险，只读优先
>
> **用户只有主号。** 瑞数（WZWS）对自动化非常敏感，反复撞主号会被风控。
> 所以本 PoC **默认只读**：打开页面、看挑战有没有自动过去、报告登录态和签到入口，
> **绝不会自动点签到提交**。
>
> - 先用只读模式，低频，**别反复跑，别循环刷新**。
> - 只有只读确认能过挑战，才考虑加 `--apply` 跑**一次**签到（仍用主号，风险自负）。
> - 脚本内置 60 秒最小间隔 + 每天 6 次上限，不是为了方便，是为了保护你的主号。

---

## 0. 这个 PoC 回答什么问题

单一问题：**容器里的 headless Chromium + stealth，能不能自动过 www.52pojie.cn 的瑞数动态防护挑战，并完成签到？**

背景（架构师评审结论）：

- 吾爱挂的是**瑞数 WZWS**，不是 Cloudflare。机制是让浏览器执行一段混淆 JS 算出一次性参数，换一个短时效的 `wzws_cid`。
- 纯 HTTP（`requests` / `curl_cffi`）算不出来 —— 实测两种客户端各 3 次，全部被拦。
- 瑞数会看 `navigator.webdriver`、CDP 痕迹、插件/字体/WebGL、headless UA 等自动化信号。
- 网上那篇「headless 全军覆没」讲的是 **CF Turnstile**，和瑞数不是一套信号，**不能照搬结论** —— 必须实测。

**为什么这不是产品承诺**：这是一次性时间盒实验。就算 headless 这次过了，也不代表长期稳定；瑞数规则一变就可能失效。通过之后要不要集成、怎么集成，是另一件事（见第 7 节）。

---

## 1. 目录与主项目完全隔离

本目录 `browser-poc/` 是实验区：

- **不改** checkin-hub 的主镜像、主 Dockerfile、`docker-compose.yml`、`requirements.txt`、任何 `checkin/sites/*.py`。
- **不进入主镜像、不进 docker-compose**：即使本目录纳入 git，也不会影响主程序和生产运行。
- `profile/`（浏览器 profile，含登录态）和 `shots/`（截图）都在 `.gitignore` 里，绝不进 git。
- 随时可以整个删掉这个目录，主项目不受任何影响。

---

## 2. 三步走

### 第 1 步：构建镜像（约 +1GB）

在本目录下执行：

```bat
docker build -t pojie-browser-poc .
```

> 镜像会拉 Chromium 和一批系统库，体积大约增加 1GB。这就是真浏览器方案的代价。

### 第 2 步：只读模式跑（默认，不点签到）

推荐用 `--env-file` 注入凭据（比 `-e` 直接写在命令行更不容易泄露到进程列表 / shell 历史）：

```bat
docker run --rm -it ^
  -v "%cd%\profile:/poc/profile" ^
  -v "%cd%\shots:/poc/shots" ^
  --env-file .env ^
  pojie-browser-poc ^
  python probe.py
```

`%cd%` 在本目录下展开。Linux/macOS 把 `%cd%` 换成 `$(pwd)`、把 `^` 换成 `\`。

`.env` 内容（**这个文件不要提交**，已进 `.gitignore`）：

```
POJIE_COOKIE=你的完整Cookie行
```

如果不需要登录态（只想先看挑战能不能过），可以省略 `--env-file`。

### 第 3 步：看结论

脚本输出一段只读报告，重点看这几行：

```
是否通过挑战 : 是
是否登录态   : 是
是否签到入口 : 是
```

只有三项都为「是」，才说明这条技术路线值得继续。然后再考虑加 `--apply` 做一次签到（见第 5 节）。

---

## 3. 风控警告（再强调一次）

- 瑞数对浏览器指纹、访问频率都敏感，**主号撞多了会被限制**。
- 只读模式也**不要连着刷**。脚本的 60 秒间隔和每日 6 次上限请当作硬约束，不要改代码绕过。
- 判定要求「连跑 3 次」是指**间隔开来的 3 次**，不是一口气连刷 3 遍。
- `--apply` 会真的提交签到，**只做一次，不重试**。做完就停，看结果，不要因为没看懂就再点一次。
- 脚本不打印 Cookie 值、不打印代理值、不打印页面正文全文、URL 也会去掉 query 参数（防止 token 进日志）。截图只存图片。

---

## 4. 两种运行模式

### headless（默认）

```bat
docker run --rm -it -v "%cd%\profile:/poc/profile" -v "%cd%\shots:/poc/shots" --env-file .env pojie-browser-poc python probe.py
```

### headed（有头，需 Xvfb）

文章说 headless 在 CF Turnstile 上全军覆没，但那不是瑞数。如果 headless 多次都过不了，可以试试有头模式，看是「指纹问题」还是「姿势问题」：

```bat
docker run --rm -it -v "%cd%\profile:/poc/profile" -v "%cd%\shots:/poc/shots" --env-file .env pojie-browser-poc xvfb-run -a python probe.py --headed
```

> 镜像里已带 Xvfb（`playwright install --with-deps chromium` 会装）。有头模式配 Xvfb 后仍可远程跑。

其它参数：

- `--max-wait 40`：等挑战自动完成的秒数上限，默认 40。前 8 秒**纯等待不交互**，之后若看到明确挑战控件才轻点一次 —— 瑞数通常只是 JS 自动算，没有勾选框。
- `--out ./shots`：截图目录。
- `--proxy socks5://host:port`：也可设 `POJIE_PROXY` 环境变量。Chromium 走 `socks5://` 默认远端 DNS，等价 `socks5h`。**日志不会打印代理值**。

---

## 5. 只有确认能过挑战，才考虑 `--apply`

```bat
docker run --rm -it ^
  -v "%cd%\profile:/poc/profile" ^
  -v "%cd%\shots:/poc/shots" ^
  --env-file .env ^
  pojie-browser-poc ^
  python probe.py --apply
```

`--apply` 做的事：在同一个浏览器上下文里，复用现有 `checkin/sites/pojie.py` 的签到思路，访问 `home.php?mod=task&do=apply&id=2` 和 `do=draw&id=2` 各**一次**，读 `#messagetext` 提示框，判定「签到成功 / 今日已签到 / 登录态失效 / 任务不存在 / 无法判断」。**不重试、不循环**。

再次强调：这一步用的是主号，瑞数可能触发风控。只读没确认能过挑战之前，**不要加这个参数**。

---

## 6. 结果在哪、怎么判断

- **报告**：直接打印在终端，字段含义见第 3 节。
- **截图**：挂在宿主 `shots/` 目录 —— `01-challenge.png`（挑战页/早期）、`02-passed.png`（通过后）。只存图，不打印 URL 参数。
- **判定标准**：间隔开连跑 3 次，**≥2 次**自动过挑战 +（若用 `--apply`）签到成功，才算 PoC 通过、值得集成。否则放弃，或退一步试有头模式（第 4 节）；有头也不行就说明这条路成本太高。

### 失败排查

| 现象 | 可能原因 | 怎么办 |
| --- | --- | --- |
| 一直「是否通过挑战：否」，超时 | 挑战没自动完成 / headless 指纹被识别 | 试有头模式；把 `--max-wait` 适当加大到 60（别反复刷） |
| 通过挑战但「是否登录态：否」 | Cookie 没预置或已过期 | 确认 `.env` 里 `POJIE_COOKIE` 是浏览器里刚复制的完整行；先在同一 profile 里手动登录一次再用 |
| 打开首页失败 | 代理出口不通 / 代理格式错 | 检查 `POJIE_PROXY`，用 `socks5://host:port`；确认该出口能正常访问 52pojie |
| 报告「挑战页截图」为空 | 页面 1 秒内就跳转或失败 | 看终端的打开失败提示，先解决网络/代理 |
| 触发「风控保护：请至少间隔 60s」 | 跑太频繁 | 等够间隔，不要删 `.last_run` 绕过 |

> 注意：profile 目录里的 `.last_run` / `.runs_YYYYMMDD` 是限频标记，**不要删**。删了就等于把保护拆了。

---

## 7. 通过之后才谈集成

PoC 通过**不代表**立刻把它塞进主项目。真要集成，那时才动主镜像，并且：

1. 做成通用的 `browser_session` 能力层（persistent profile + stealth + 代理），而不是给吾爱写死一套；
2. 按站点 **opt-in**，只有确实需要过动态防护的站点才启用；
3. 评估 +1GB 镜像体积、启动耗时、无头长期稳定性，以及主号风控的可接受度。

在那之前，主项目保持原样。

---

## 8. 本机（Windows）验证说明

PoC 脚本依赖 Playwright，装在容器里。**本机没装 Playwright 属正常**，不影响以下验证：

```bat
python -m compileall -q browser-poc
python browser-poc\probe.py --help
```

重型 import 放在 `main()` 里延迟执行，所以 `--help` 在本机也能正常解析。真正跑挑战请在容器里（第 2 节）。
