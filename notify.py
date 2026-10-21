"""notify —— 多渠道推送。任一道失败只记日志，不影响监控主循环。"""

from __future__ import annotations

import json
import logging
import smtplib
import subprocess
import sys
import time
from email.mime.text import MIMEText
from email.header import Header

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.timeout = 15
        self.channels = [k for k, v in self.cfg.items()
                         if isinstance(v, dict) and v.get("enabled")]
        if not self.channels:
            log.warning("未配置任何通知渠道，仅打印到控制台")

    # ---------------------------------------------------------------- 入口
    def send(self, title: str, body: str, url: str = "") -> dict[str, str]:
        results: dict[str, str] = {}
        for name in self.channels:
            fn = getattr(self, f"_ch_{name}", None)
            if fn is None:
                results[name] = "unknown-channel"
                continue
            try:
                fn(self.cfg[name], title, body, url)
                results[name] = "ok"
            except Exception as e:  # noqa: BLE001
                results[name] = f"{type(e).__name__}: {e}"
                log.error("通知渠道 %s 失败: %s", name, e)

        # 控制台兜底始终打印
        print("\n" + "=" * 68)
        print(f"【{title}】")
        print(body)
        if url:
            print(url)
        print("=" * 68)
        return results

    # ---------------------------------------------------------------- 渠道
    def _ch_serverchan(self, c: dict, title: str, body: str, url: str):
        """Server 酱 Turbo。key 形如 SCTxxxx。"""
        if url:
            body = f"{body}\n\n[打开活动]({url})"
        r = requests.post(f"https://sctapi.ftqq.com/{c['key']}.send",
                          data={"title": title[:32], "desp": body},
                          timeout=self.timeout)
        r.raise_for_status()

    def _ch_bark(self, c: dict, title: str, body: str, url: str):
        """Bark (iOS)。server 默认 api.day.app。"""
        server = c.get("server", "https://api.day.app").rstrip("/")
        payload = {"title": title, "body": body, "group": c.get("group", "到梦空间"),
                   "level": c.get("level", "timeSensitive"), "sound": c.get("sound", "bell")}
        if url:
            payload["url"] = url
        r = requests.post(f"{server}/{c['key']}", json=payload, timeout=self.timeout)
        r.raise_for_status()

    def _ch_dingtalk(self, c: dict, title: str, body: str, url: str):
        if url:
            body = f"{body}\n\n[{url}]({url})"
        payload = {"msgtype": "markdown",
                   "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
        r = requests.post(c["webhook"], json=payload, timeout=self.timeout)
        r.raise_for_status()
        if r.json().get("errcode") not in (0, None):
            raise RuntimeError(r.text[:200])

    def _ch_wecom(self, c: dict, title: str, body: str, url: str):
        if url:
            body = f"{body}\n\n[查看详情]({url})"
        payload = {"msgtype": "markdown",
                   "markdown": {"content": f"### {title}\n{body}"}}
        r = requests.post(c["webhook"], json=payload, timeout=self.timeout)
        r.raise_for_status()

    def _ch_telegram(self, c: dict, title: str, body: str, url: str):
        text = f"*{title}*\n{body}"
        if url:
            text += f"\n[打开]({url})"
        r = requests.post(
            f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
            json={"chat_id": c["chat_id"], "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": False}, timeout=self.timeout)
        r.raise_for_status()

    def _ch_email(self, c: dict, title: str, body: str, url: str):
        if url:
            body += f"\n\n{url}"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = c["user"]
        msg["To"] = ", ".join(c["to"]) if isinstance(c["to"], list) else c["to"]
        host, port = c.get("smtp_host", "smtp.qq.com"), int(c.get("smtp_port", 465))
        if c.get("ssl", True):
            srv = smtplib.SMTP_SSL(host, port, timeout=self.timeout)
        else:
            srv = smtplib.SMTP(host, port, timeout=self.timeout)
            srv.starttls()
        try:
            srv.login(c["user"], c["password"])
            srv.send_message(msg)
        finally:
            srv.quit()

    def _ch_webhook(self, c: dict, title: str, body: str, url: str):
        """通用 JSON webhook，方便接 n8n / 自建服务。"""
        r = requests.post(c["url"], json={"title": title, "body": body, "url": url,
                                          "ts": int(time.time())}, timeout=self.timeout)
        r.raise_for_status()

    def _ch_console(self, c: dict, title: str, body: str, url: str):
        """仅控制台（由 send 统一打印）。可选响铃。"""
        if c.get("beep") and sys.platform == "win32":
            try:
                subprocess.run(["powershell", "-NoProfile", "-c",
                                "[console]::beep(880,300);[console]::beep(1170,300)"],
                               timeout=5, capture_output=True)
            except Exception:  # noqa: BLE001
                pass
