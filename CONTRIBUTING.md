# 参与开发

欢迎提 Issue 和 PR。改动前建议先开一个 Issue 说清楚要做什么，避免白做。

## 目录结构

```
checkin/
├─ main.py                  # 入口：读配置 → 跑站点 → 汇总 → 写状态 → 推送
├─ checkin/
│  ├─ core.py               # Site 基类、cookie 解析、重试、日志脱敏、verify_account
│  ├─ config.py             # 环境变量 / config.json / 面板设置 三套来源
│  ├─ settings.py           # data/settings.json（面板里保存的账号与代理）
│  ├─ notify.py             # 8 个推送渠道
│  ├─ store.py              # data/status.json、runs.json
│  └─ sites/                # 一个站点一个文件
├─ panel/
│  ├─ server.py             # 标准库 HTTP 服务 + 登录鉴权 + 设置接口
│  ├─ index.html            # 状态页（不加载任何外部资源）
│  ├─ settings.html         # 设置页：口令 / cookie / 代理 / 检测
│  └─ login.html
├─ runtime189/              # 189 的 Node 桥接（cloud189-sdk）
├─ .github/workflows/       # checkin.yml 定时签到、docker.yml 构建镜像到 GHCR
├─ Dockerfile               # Python + Node 双运行时
└─ docker-compose.yml
```

## 本地开发

```bash
pip install -r requirements.txt
cd runtime189 && npm ci && cd ..

export V2EX_A2=xxx           # 或用 .env
python main.py --list        # 确认站点被识别
python main.py --site v2ex --no-notify --debug
python -m panel.server       # 面板，默认 8080
```

`--debug` 会打印完整异常堆栈。默认情况下异常会被翻译成中文提示（代理不可用、TLS 被拦、超时等）。

## 加一个站点

1. 新建 `checkin/sites/xxx.py`，继承 `Site`：

```python
from checkin.core import Line, Site, friendly_error


class XxxSite(Site):
    key = "XXX"                    # 与配置 KEY 一致，大写
    name = "站点中文名"
    fields = ("cookie",)           # 环境变量后缀：XXX_COOKIE
    required = ("cookie",)
    doc = "cookie 至少包含 foo 字段"

    def run(self) -> list[Line]:
        s = self.session()          # 自动带上 cookie 和该账号的 proxy
        s.get("https://example.com/checkin")
        return [Line("签到", "成功")]
```

2. 在 `checkin/sites/__init__.py` 的 `SITES` 列表里加一行，并确保 `BY_KEY` 能按小写 key 找到它
3. 需要凭据的，在 `.github/workflows/checkin.yml` 的 `env:` 里补 `XXX_COOKIE: ${{ secrets.XXX_COOKIE }}`，同时更新 `.env.example` 和 `config.example.json`
4. `python main.py --list` 确认能识别，再 `python main.py --site xxx --no-notify` 实测

约定：

- `run()` 正常返回 = 成功，抛异常 = 失败。主控会捕获、汇总、推送，一个站点崩了不影响整轮
- 结果用 `Line(标签, 值)` 返回，面板会直接渲染成两列表格，别自己拼字符串
- 日志里不要出现 cookie / 密码原文，`core.py` 的 `redact()` 会兜底，但别依赖它
- 请求要带间隔（1~2 秒）和重试，站点风控比速度重要
- 代理、多账号、备注（`_NAME`）、面板统计都是框架自动支持的，站点代码里不要处理

### 顺便支持"检测凭据"（可选）

面板上每个账号有个「检测」按钮，调用的是站点的 `verify()`。想让自己加的站点也能检测，就实现它：

```python
    def verify(self) -> tuple[bool, str]:
        """只读检查，绝不能顺手签到或产生任何写操作。"""
        session = self.session(headers={"Referer": "https://example.com/"})
        data = session.get("https://example.com/api/me", timeout=20).json()
        if data.get("code") == 0:
            return True, f"Cookie 有效：{data['data']['name']}"
        return False, "Cookie 已失效（接口返回未登录）"
```

约定：

- 返回 `(True, 说明)` 或 `(False, 说明)`；说明里**不要**出现 cookie 原文
- 网络异常直接让它抛出去，框架会归类成"无法确认"，不会误报成失效
- 没有稳定的只读接口就别实现 —— 默认实现会抛 `VerifyUnsupported`，面板显示"暂不支持"，比给假结论好

## 代码风格

- Python 3.10+，只用标准库 + `requests`，不要为了一个小功能引入新依赖
- 注释和提示信息用中文，面向的是中文用户
- 面板前端不引入任何 CDN 资源（CSP 是 `default-src 'self'`），脚本和样式内联
- 涉及凭据的地方，先想一遍"这段会不会进日志 / 进 git / 进镜像"

## 提 PR 前

```bash
python -m compileall -q checkin panel main.py
node --check runtime189/index.js
sh -n docker/entrypoint.sh
```

再确认没有把 `.env`、`config.json`、`data/`、`__pycache__` 带进提交。

## 维护者备忘

**用网页上传代码时**（不熟 git 的贡献者可能会问）：

- 拖**目录里的内容**，不要拖目录本身，否则仓库里会多套一层，Actions 找不到 `main.py`
- `.github/`、`.gitignore`、`.dockerignore`、`.gitattributes`、`.env.example` 这些点开头的必须一起传上去，传完回首页确认根目录有 `.github`
- 网页上传**不读 `.gitignore`**，传之前先把 `.env`、`config.json`、`data/`、`__pycache__` 清掉
- 漏了 `.github`：`Add file → Create new file`，文件名直接输入 `.github/workflows/checkin.yml` 即可自动建目录
