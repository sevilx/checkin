"""通知推送：填了哪个环境变量就走哪个渠道，全都没填就只打印到日志。

支持 Telegram / Server酱³ / PushPlus / 企业微信机器人 / 飞书机器人 / Bark / Gotify / ntfy。
任一渠道失败只记日志，不影响其它渠道，也不改变签到任务的退出码。
"""

from __future__ import annotations

import json
import os

import requests

TIMEOUT = 15


def _post(url: str, **kwargs) -> bool:
    try:
        resp = requests.post(url, timeout=TIMEOUT, **kwargs)
        if resp.status_code >= 400:
            print(f"[notify] {url.split('?')[0]} 返回 {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] {url.split('?')[0]} 推送异常: {exc}")
        return False


def _telegram(title: str, body: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    api = os.environ.get("TELEGRAM_API_HOST", "https://api.telegram.org").rstrip("/")
    return _post(
        f"{api}/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": f"{title}\n\n{body}", "disable_web_page_preview": "true"},
    )


def _server_chan(title: str, body: str) -> bool:
    key = os.environ.get("SC3_PUSH_KEY") or os.environ.get("SERVERCHAN_KEY")
    if not key:
        return False
    if key.startswith("sctp"):
        # Server酱³
        return _post(
            f"https://{key.split('t')[0]}.push.ft07.com/send/{key}.send",
            data={"title": title, "desp": body},
        )
    return _post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": body})


def _pushplus(title: str, body: str) -> bool:
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        return False
    payload = {"token": token, "title": title, "content": body.replace("\n", "<br>"), "template": "txt"}
    if os.environ.get("PUSHPLUS_TOPIC"):
        payload["topic"] = os.environ["PUSHPLUS_TOPIC"]
    return _post("https://www.pushplus.plus/send", json=payload)


def _wecom(title: str, body: str) -> bool:
    url = os.environ.get("WECOM_WEBHOOK")
    if not url:
        return False
    return _post(url, json={"msgtype": "text", "text": {"content": f"{title}\n\n{body}"}})


def _feishu(title: str, body: str) -> bool:
    url = os.environ.get("FEISHU_WEBHOOK")
    if not url:
        return False
    payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}}
    return _post(url, json=payload)


def _bark(title: str, body: str) -> bool:
    url = os.environ.get("BARK_URL")
    if not url:
        return False
    target = url.rstrip("/")
    if not target.endswith("/push"):
        target = f"{target}/{title}"
        return _post(target, data={"body": body})
    return _post(target, json={"title": title, "body": body})


def _gotify(title: str, body: str) -> bool:
    url = os.environ.get("GOTIFY_URL")
    token = os.environ.get("GOTIFY_TOKEN")
    if not (url and token):
        return False
    return _post(
        f"{url.rstrip('/')}/message?token={token}",
        json={"title": title, "message": body, "priority": int(os.environ.get("GOTIFY_PRIORITY", "5"))},
    )


def _ntfy(title: str, body: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": os.environ.get("NTFY_PRIORITY", "default")}
    return _post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)


CHANNELS = (_telegram, _server_chan, _pushplus, _wecom, _feishu, _bark, _gotify, _ntfy)


def send(title: str, body: str) -> list[str]:
    """按配置推送，返回实际成功的渠道名。"""
    sent = []
    for channel in CHANNELS:
        try:
            if channel(title, body):
                sent.append(channel.__name__.lstrip("_"))
        except Exception as exc:  # noqa: BLE001
            print(f"[notify] {channel.__name__} 异常: {exc}")
    if not sent:
        print("[notify] 未配置任何推送渠道，仅输出到日志")
    else:
        print(f"[notify] 已推送: {', '.join(sent)}")
    return sent


def dump_debug(title: str, body: str) -> None:
    print(f"\n===== {title} =====\n{body}\n")
    print(json.dumps({"channels_configured": [
        name for name, key in (
            ("telegram", "TELEGRAM_BOT_TOKEN"),
            ("serverchan", "SC3_PUSH_KEY"),
            ("pushplus", "PUSHPLUS_TOKEN"),
            ("wecom", "WECOM_WEBHOOK"),
            ("feishu", "FEISHU_WEBHOOK"),
            ("bark", "BARK_URL"),
            ("gotify", "GOTIFY_URL"),
            ("ntfy", "NTFY_TOPIC"),
        ) if os.environ.get(key)
    ]}, ensure_ascii=False))
