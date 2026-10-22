"""天翼云盘（189）：调用本仓库的 Node 小工具（cloud189-sdk）完成签到。

189 的登录要 RSA 加密 + 设备指纹，官方没有可用的 Cookie 接口，所以这里不重写协议，
而是复用 wes-lin/Cloud189Checkin 使用的 cloud189-sdk：Python 负责调度和汇总，
Node 只负责"登录 + 签到 + 报容量变化"，结果用临时 JSON 文件回传。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..core import Line, Site

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "runtime189" / "index.js"


class Cloud189(Site):
    key = "TY189"
    name = "天翼云盘 189"
    fields = ("username", "password")
    required = ("username", "password")
    doc = "填天翼账号手机号 + 密码（不建议用 Cookie，189 走的是账号密码登录）"

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
                "TY_USERNAME": str(self.account.get("username")),
                "TY_PASSWORD": str(self.account.get("password")),
                "TY_ACCOUNTS": "",
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
            raise RuntimeError(str(data.get("error") or "未知错误"))

        lines = [Line(name, value) for name, value in data.get("lines") or []]
        if self.account.get("proxy"):
            lines.append(Line("代理", "189 走 SDK 登录，代理设置对它不生效；国内站点直连即可"))
        return lines
