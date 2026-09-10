"""ServerChan Turbo transport with a durable local free-tier attempt budget.

Construction and quota_status never contact ServerChan. A successful send only
confirms API acceptance; it does not prove that WeChat delivered the message.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import ssl
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

from .notifier import NotificationError, _NoRedirect


DAILY_LIMIT = 5
_DATABASE = "serverchan-quota.sqlite3"
_WECHAT_CHANNEL = "9"
_MAX_RESPONSE_BYTES = 65_536
_KEY = re.compile(r"SCT[A-Za-z0-9]{8,253}\Z")
_PUSHID = re.compile(r"[1-9][0-9]{0,63}\Z")
# Present-day Asia/Shanghai is UTC+08:00. A fixed zone also works on Windows
# installations without a system IANA database or third-party tzdata package.
_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def validate_sendkey(sendkey: str) -> str:
    """Return a normalized Turbo key, without exposing invalid input in errors."""
    if not isinstance(sendkey, str) or not _KEY.fullmatch(sendkey.strip()):
        raise NotificationError("请填写以 SCT 开头的 Server酱 Turbo SendKey；不支持 SC3 App 密钥。")
    return sendkey.strip()


def _today() -> str:
    return datetime.now(_SHANGHAI).date().isoformat()


def _quota(sendkey: str, data_dir: str | Path, *, consume: bool) -> dict:
    fingerprint = hashlib.sha256(validate_sendkey(sendkey).encode("ascii")).hexdigest()
    connection = None
    try:
        directory = Path(data_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / _DATABASE
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA synchronous=FULL")
        # The write lock covers initialization, reading and incrementing, also
        # across separate processes. Commit the reservation before any network.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS daily_attempts ("
            "key_hash TEXT NOT NULL, day TEXT NOT NULL, "
            "used INTEGER NOT NULL CHECK(used BETWEEN 0 AND 5), "
            "PRIMARY KEY(key_hash, day))"
        )
        today = _today()
        row = connection.execute(
            "SELECT used FROM daily_attempts WHERE key_hash=? AND day=?",
            (fingerprint, today),
        ).fetchone()
        used = row[0] if row else 0
        if type(used) is not int or not 0 <= used <= DAILY_LIMIT:
            raise NotificationError("Server酱额度记录异常，本次没有发送，请检查本地数据目录。")
        if consume:
            if used >= DAILY_LIMIT:
                raise NotificationError(
                    "Server酱本程序今日 5 次免费发送尝试已用完；测试和失败也计数，"
                    "北京时间次日恢复，本次没有发送。"
                )
            connection.execute(
                "INSERT INTO daily_attempts(key_hash, day, used) VALUES(?, ?, 1) "
                "ON CONFLICT(key_hash, day) DO UPDATE SET used=used+1",
                (fingerprint, today),
            )
            used += 1
        connection.commit()
        return {"today": today, "used": used, "remaining": DAILY_LIMIT - used, "limit": DAILY_LIMIT}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise NotificationError("无法读取或保存 Server酱本地额度记录，本次没有发送。") from None
    finally:
        if connection is not None:
            connection.close()


def quota_status(sendkey: str, data_dir: str | Path) -> dict:
    """Read this application's persisted daily attempt count; no API request."""
    return _quota(sendkey, data_dir, consume=False)


class ServerChanNotifier:
    """One request per send, with no retry even when the outcome is unknown."""

    def __init__(self, sendkey: str, data_dir: str | Path, timeout: float = 20):
        self._sendkey = validate_sendkey(sendkey)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise NotificationError("Server酱请求超时必须是有限正数。")
        try:
            valid_timeout = math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise NotificationError("Server酱请求超时必须是有限正数。")
        self.timeout = float(timeout)
        self.data_dir = data_dir
        try:
            context = ssl.create_default_context()
            # Some conda installations omit the machine's managed CA bundle.
            # Add it to the default trust store; never disable verification.
            system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
            if system_ca.is_file():
                context.load_verify_locations(cafile=str(system_ca))
        except (OSError, ssl.SSLError):
            raise NotificationError("无法加载 Server酱 HTTPS 信任证书，本次没有发送。") from None
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def send(self, title: str, content: str) -> str:
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 32
            or any(ord(char) < 32 or ord(char) == 127 for char in title)
        ):
            raise NotificationError("Server酱标题须为 1～32 个字符且不能包含换行或控制字符。")
        if not isinstance(content, str) or not content.strip():
            raise NotificationError("Server酱推送内容不能为空。")
        try:
            # Channel 9 is the official 方糖服务号. An account's mutable default
            # might instead route to ClawBot or an independent app.
            payload = urlencode({"title": title, "desp": content, "noip": 1, "channel": _WECHAT_CHANNEL}).encode("ascii")
        except UnicodeError:
            raise NotificationError("Server酱推送文本包含无效的 Unicode 字符。") from None
        request = Request(
            f"https://sctapi.ftqq.com/{self._sendkey}.send",
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "flight-price-watcher",
            },
            method="POST",
        )
        _quota(self._sendkey, self.data_dir, consume=True)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise NotificationError("Server酱返回 HTTP 错误，本次已计入额度；请检查服务及账户状态。")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError:
            raise NotificationError("Server酱返回 HTTP 错误，本次已计入额度；请检查服务及账户状态。") from None
        except (URLError, OSError, HTTPException):
            raise NotificationError(
                "Server酱网络失败或超时，受理结果未知且本次已计入额度；"
                "同次调用不会立即重试，后续监控轮次可能再次提交并产生重复；请到 Server酱后台确认。"
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise NotificationError("Server酱响应过大，受理结果未知且本次已计入额度。")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise NotificationError("Server酱响应格式无效，受理结果未知且本次已计入额度。") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise NotificationError("Server酱响应格式无效，受理结果未知且本次已计入额度。")
        if result["code"] != 0:
            raise NotificationError("Server酱拒绝请求，本次已计入额度；请检查 SendKey、通道及账户额度。")
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or type(data.get("errno")) is not int
            or data["errno"] != 0
            or data.get("error") != "SUCCESS"
        ):
            raise NotificationError("Server酱未确认请求受理，本次已计入额度；请到 Server酱后台确认。")
        pushid = data.get("pushid")
        if type(pushid) is int and 0 < pushid < 10**64:
            pushid = str(pushid)
        if not isinstance(pushid, str) or not _PUSHID.fullmatch(pushid):
            raise NotificationError("Server酱未返回有效流水号，受理结果未知且本次已计入额度。")
        return f"accepted:serverchan:{pushid}"
