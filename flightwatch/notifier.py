"""Send plain-text WeChat alerts through the documented PushPlus API.

PushPlus acknowledges requests asynchronously. ``send`` returns
``accepted:<receipt>``; this is not a delivery confirmation. Its current delivery
query API requires an additional access-key and cannot use a message token.
See https://pushplus.plus/doc/guide/api.html and
https://pushplus.plus/doc/guide/openApi.html .
"""

from __future__ import annotations

import json
import math
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


_SEND_URL = "https://www.pushplus.plus/send"
_MAX_RESPONSE_BYTES = 65_536
_RECEIPT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class NotificationError(Exception):
    """A safe-to-display notification error containing no request credentials."""


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward a credential-bearing request to another endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PushPlusNotifier:
    """A token-only PushPlus client; no automatic retries or delivery claims.

    A timeout can happen after the service accepted a request. Retrying it may
    duplicate a notification, so retries belong to the calling application's
    persistent alert policy rather than this transport.
    """

    def __init__(self, token: str, timeout: float = 20):
        if not isinstance(token, str) or not token.strip():
            raise NotificationError("缺少 PushPlus token，请配置 PUSHPLUS_TOKEN。")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise NotificationError("PushPlus 请求超时必须是正数。")
        try:
            valid_timeout = math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise NotificationError("PushPlus 请求超时必须是有限正数。")
        self._token = token.strip()
        self.timeout = float(timeout)
        self._opener = build_opener(_NoRedirect())

    def send(self, title: str, content: str) -> str:
        """Submit one WeChat message and return an ``accepted:`` receipt.

        No messages are sent during construction. Remote error text, exception
        strings, URLs, and response bodies are deliberately excluded from errors.
        """
        if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
            raise NotificationError("推送标题必须为文本，推送内容不能为空。")
        try:
            payload = json.dumps(
                {
                    "token": self._token,
                    "title": title,
                    "content": content,
                    "channel": "wechat",
                    "template": "txt",
                },
                ensure_ascii=False,
            ).encode("utf-8")
        except UnicodeError:
            raise NotificationError("推送文本包含无效的 Unicode 字符。") from None
        request = Request(
            _SEND_URL,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "flight-price-watcher/1.0",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise NotificationError("PushPlus 返回 HTTP 错误，请检查服务状态和账户设置。")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError:
            raise NotificationError("PushPlus 返回 HTTP 错误，请检查服务状态和账户设置。") from None
        except (URLError, OSError, HTTPException):
            raise NotificationError(
                "PushPlus 网络请求失败或超时，受理结果未知；请到 PushPlus 后台确认。"
            ) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise NotificationError("PushPlus 响应过大，无法确认受理结果。")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise NotificationError("PushPlus 响应格式无效，无法确认受理结果。") from None
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise NotificationError("PushPlus 响应格式无效，无法确认受理结果。")
        if result["code"] != 200:
            raise NotificationError("PushPlus 拒绝推送请求，请检查 token、关注状态及账户额度。")
        receipt = result.get("data")
        if (
            not isinstance(receipt, str)
            or not _RECEIPT.fullmatch(receipt)
            or self._token in receipt
        ):
            raise NotificationError("PushPlus 未返回有效流水号，无法确认受理结果。")
        return f"accepted:{receipt}"
