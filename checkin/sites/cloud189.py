"""天翼云盘（189）：调用本仓库的 Node 小工具（cloud189-sdk）完成签到。

189 的登录要 RSA 加密 + 设备指纹，官方没有可用的 Cookie 接口，所以这里不重写协议，
而是复用 wes-lin/Cloud189Checkin 使用的 cloud189-sdk：Python 负责调度和汇总，
Node 只负责"登录 + 签到 + 报容量变化"，结果用临时 JSON 文件回传。

两条比原版多做的事：
* 会话缓存：token 落到 data/token189/，一次登录能用 6 天，不再天天重新登录
  （189 对频繁登录非常敏感，这是 "Can not get session" 最主要的诱因）；
* 备用通道：账号密码登录被设备校验挡住时，可以只填 SSON（浏览器里 e.189.cn 的 SSON cookie 值）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..core import Line, Site
from ..store import DATA_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "runtime189" / "index.js"
TOKEN_DIR = DATA_DIR / "token189"


class Cloud189(Site):
    key = "TY189"
    name = "天翼云盘 189"
    fields = ("username", "password", "sson")
    required = ()
    doc = (
        "填天翼账号手机号 + 密码；如果一直被设备校验挡住，可以只填 SSON："
        "浏览器登录 cloud.189.cn → F12 → Application → Cookies → 复制 SSON 的值"
    )

    def __init__(self, account: dict, index: int = 0):
        super().__init__(account, index)
        has_password = bool(self.account.get("username") and self.account.get("password"))
        has_sson = bool(self.account.get("sson"))
        if not has_password and not has_sson:
            raise ValueError("缺少必填配置: username / password（或只用 sson 登录）")

    def run(self) -> list[Line]:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("未找到 node，189 签到需要 Node.js（Actions 已自动装，本地请装 Node 18+）")
        if not SCRIPT.exists():
            raise RuntimeError(f"缺少运行时脚本: {SCRIPT}")
        if not (ROOT / "runtime189" / "node_modules" / "cloud189-sdk").exists():
            raise RuntimeError("未安装 cloud189-sdk，请先执行 npm install --prefix runtime189")

        env = os.environ.copy()
        env.update(
            {
                "TY_USERNAME": str(self.account.get("username") or ""),
                "TY_PASSWORD": str(self.account.get("password") or ""),
                "TY_SSON": str(self.account.get("sson") or ""),
                "TY_ACCOUNTS": "",
                "TY_TOKEN_DIR": str(TOKEN_DIR),
                "NO_COLOR": "1",
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "result.json"
            env["TY_RESULT_FILE"] = str(result_file)
            proc = subprocess.run(
                [node, str(SCRIPT)],
                cwd=str(ROOT / "runtime189"),
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
            )
            if not result_file.exists():
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
                raise RuntimeError("189 脚本未产出结果: " + " | ".join(detail))
            data = json.loads(result_file.read_text(encoding="utf-8"))

        if not data.get("ok"):
            error = str(data.get("error") or "未知错误")
            # SDK 的报错会把真实原因吞掉，只留一句 "Can not get session."，
            # 把它的日志尾巴带上，用户和我们都好判断
            tail = _log_tail(proc)
            if tail and "Can not get session" in error:
                error += f"\nSDK 日志：{tail}"
            raise RuntimeError(error)

        lines = [Line(name, value) for name, value in data.get("lines") or []]
        if self.account.get("proxy"):
            lines.append(Line("代理", "189 走 SDK 登录，代理设置对它不生效；国内站点直连即可"))
        return lines


def _log_tail(proc, limit: int = 400) -> str:
    """取 SDK 日志的最后一段，过滤掉空行。"""
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    joined = " | ".join(rows[-4:])
    return joined[-limit:]
