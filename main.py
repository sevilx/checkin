#!/usr/bin/env python3
"""checkin 入口：读取配置 → 逐个站点签到 → 汇总输出 → 推送通知。

用法：
    python main.py                     # 跑所有已配置站点
    python main.py --site v2ex,nodeseek
    python main.py --list              # 看支持哪些站点、要配哪些环境变量
    python main.py --no-notify --debug # 本地调试：不推送、打印详情
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from datetime import datetime

from checkin import store
from checkin.config import collect_accounts, enabled_sites, load_env_file, load_file_config
from checkin.core import AccountResult, friendly_error, mask, redact
from checkin.notify import send
from checkin.sites import BY_KEY, SITES

TZ_LABEL = os.environ.get("TZ", "Asia/Shanghai")


def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _alarm_start(seconds: int) -> None:
    if not seconds or not hasattr(signal, "SIGALRM"):
        return

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        raise TimeoutError(f"超过 {seconds} 秒未完成")

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _alarm_cancel() -> None:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)


def run_account(site_cls, account: dict, index: int) -> AccountResult:
    label = str(account.get("name") or account.get("remark") or "")
    if not label:
        account_id = account.get("username") or account.get("userName") or ""
        label = mask(str(account_id), 3, 4) if account_id else ""
    result = AccountResult(site=site_cls.name, label=label)
    started = time.time()
    try:
        site = site_cls(account, index)
        _alarm_start(getattr(site_cls, "timeout", 300))
        try:
            result.lines = site.run()
        finally:
            _alarm_cancel()
        result.ok = True
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = friendly_error(exc)
        if os.environ.get("CHECKIN_DEBUG"):
            traceback.print_exc()
    result.elapsed = time.time() - started
    return result


def build_summary(results: list[AccountResult]) -> tuple[str, str]:
    ok = sum(1 for r in results if r.ok)
    total = len(results)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"【签到汇总】{ok}/{total} 成功 · {stamp}"

    body: list[str] = []
    for item in results:
        head = f"{'✅' if item.ok else '❌'} {item.site}"
        if item.label:
            head += f"（{item.label}）"
        body.append(head)
        for line in item.text().splitlines():
            body.append(f"   {line}")
    body.append("")
    if total:
        failed = [f"{r.site}{'（' + r.label + '）' if r.label else ''}" for r in results if not r.ok]
        body.append(f"耗时 {sum(r.elapsed for r in results):.1f} 秒")
        if failed:
            body.append("需要处理：" + "、".join(failed))
    return title, "\n".join(body)


def write_step_summary(results: list[AccountResult]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    ok = sum(1 for r in results if r.ok)
    rows = ["| 站点 | 账号 | 结果 | 详情 |", "| --- | --- | --- | --- |"]
    for item in results:
        detail = "<br>".join(item.text().splitlines()).replace("|", "\\|")
        rows.append(
            f"| {item.site} | {item.label or '-'} | {'✅' if item.ok else '❌'} | {detail} |"
        )
    content = f"## 签到结果 {ok}/{len(results)}\n\n" + "\n".join(rows) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        print(f"[summary] 写入 step summary 失败: {exc}")


def list_sites() -> None:
    print("支持的站点：\n")
    for site in SITES:
        fields = " / ".join(f"{site.key}_{f.upper()}" for f in tuple(site.fields) + ("proxy",))
        print(f"  {site.key:<10} {site.name:<14} {fields}")
        if site.doc:
            print(f"  {'':<10} {site.doc}")
    print("\n多账号写法：V2EX_COOKIE_1 / V2EX_COOKIE_2 …；或在 config.json 里放数组。")
    print("代理写法：V2EX_PROXY=http://192.168.1.2:7890，或全局 GLOBAL_PROXY=...（189 除外，见 README）")


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = argparse.ArgumentParser(description="多站点自动签到")
    parser.add_argument("--site", help="只跑指定站点，逗号分隔，如 v2ex,nodeseek")
    parser.add_argument("--list", action="store_true", help="列出支持的站点")
    parser.add_argument("--no-notify", action="store_true", help="不推送通知")
    parser.add_argument("--no-store", action="store_true", help="不写 data/status.json（面板就没数据）")
    parser.add_argument("--debug", action="store_true", help="打印异常堆栈")
    args = parser.parse_args(argv)

    if args.list:
        list_sites()
        return 0

    if args.debug:
        os.environ["CHECKIN_DEBUG"] = "1"

    load_env_file()
    file_cfg = load_file_config()

    picked: list[str] | None = None
    if args.site:
        picked = [item.strip().lower() for item in args.site.split(",") if item.strip()]
    else:
        picked = enabled_sites()

    targets = []
    for site_cls in SITES:
        if picked and site_cls.key.lower() not in picked:
            continue
        targets.append(site_cls)

    if picked:
        unknown = [key for key in picked if key not in BY_KEY]
        if unknown:
            print(f"[warn] 不认识的站点已忽略: {', '.join(unknown)}")

    print(f"[checkin] 开始执行，时区 {TZ_LABEL}，站点 {len(targets)} 个")

    started = time.time()
    results: list[AccountResult] = []
    secrets: list[str] = []

    for site_cls in targets:
        accounts = collect_accounts(site_cls, file_cfg)
        if not accounts:
            print(f"[skip] {site_cls.name}：未配置（{site_cls.key}_* 环境变量或 config.json）")
            continue
        for index, account in enumerate(accounts):
            secrets.extend(str(v) for k, v in account.items() if k in ("cookie", "password", "a2") and v)
            result = run_account(site_cls, account, index)
            results.append(result)
            print(f"\n--- {site_cls.name} #{index + 1} ---")
            print(redact(result.text(), secrets))

    if not results:
        print("\n[checkin] 没有任何站点被配置，退出。先用 --list 看要配哪些变量。")
        return 0

    title, body = build_summary(results)
    print(f"\n{title}\n{body}")

    elapsed = time.time() - started
    write_step_summary(results)

    if not args.no_store:
        try:
            store.save_results(results, elapsed)
        except Exception as exc:  # noqa: BLE001
            print(f"[store] 写入运行状态失败（不影响签到）: {exc}")

    if not args.no_notify:
        send(title, body)

    failed = [r for r in results if not r.ok]
    if failed and os.environ.get("FAIL_ON_ERROR", "true").lower() != "false":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
