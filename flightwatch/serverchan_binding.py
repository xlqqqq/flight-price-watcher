"""User-triggered ServerChan website QR authorization, with RAM-only sessions.

The endpoints match sct.ftqq.com's public login flow. This module neither sends
notifications nor completes account registration or purchases on the user's
behalf. Only ``confirm`` returns the validated SendKey to its local caller.
"""

from __future__ import annotations

import base64
import json
import math
import re
import ssl
import struct
import threading
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from .notifier import NotificationError, _NoRedirect
from .serverchan_notifier import validate_sendkey


_SIGNIN = "https://sctapi.ftqq.com/user/signin"
_CHECK = "https://sctapi.ftqq.com/sso/check"
_MAX_JSON = 65_536
_MAX_IMAGE = 524_288
_FIRST_SETUP = "已扫码登录，但尚未完成首次开通。请到 https://sct.ftqq.com/ 完成免费服务号开通，再重新扫码绑定。"


class _BindingError(Exception):
    pass


def _text(value, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and all(32 < ord(char) < 127 for char in value)
    )


def _level(value) -> int | None:
    # The website compares database values numerically. Support only canonical
    # integer JSON numbers or digit strings; never truthy arbitrary objects.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
        value = int(value)
    return value if type(value) is int and 0 <= value <= 10000 else None


class ServerChanBinding:
    def __init__(self, timeout: float = 10):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
            or not math.isfinite(timeout)
        ):
            raise ValueError("扫码请求超时须为 0～120 秒内的有限正数。")
        self.timeout = float(timeout)
        ca_file = Path("/etc/ssl/certs/ca-certificates.crt")
        context = ssl.create_default_context(cafile=str(ca_file) if ca_file.is_file() else None)
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        self._lock = threading.RLock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._qr_image: str | None = None
        self._state = "idle"
        self._message = "请点击生成二维码，用手机微信扫码绑定。"

    def _clear_session(self):
        self._token = None
        self._expires_at = 0.0
        self._qr_image = None

    def cancel(self):
        """Forget an unfinished authorization without contacting the provider."""
        with self._lock:
            self._clear_session()
            self._state = "idle"
            self._message = "扫码会话已清除，请重新生成二维码。"

    def _expire(self):
        if self._token is not None and time.monotonic() >= self._expires_at:
            self._clear_session()
            self._state = "expired"
            self._message = "二维码已过期，请重新生成并扫码。"

    def status(self) -> dict:
        """Public view: no session token, ticket, user profile or SendKey."""
        with self._lock:
            self._expire()
            return {
                "state": self._state,
                "message": self._message,
                "qr_image": self._qr_image,
                "expires_in": max(0, math.ceil(self._expires_at - time.monotonic())) if self._token else 0,
            }

    def _request(self, url: str, *, payload: dict | None = None, limit: int = _MAX_JSON):
        request = Request(
            url,
            data=urlencode(payload).encode("ascii") if payload is not None else None,
            headers={
                "User-Agent": "flight-price-watcher",
                "Accept": "application/json" if limit == _MAX_JSON else "image/png,image/jpeg",
                **({"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"} if payload is not None else {}),
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                # Redirects are blocked by the handler and checked again here.
                if response.status != 200 or response.geturl() != url:
                    raise _BindingError("扫码服务返回异常，请重新生成二维码或到 Server酱官网绑定。")
                raw = response.read(limit + 1)
                media_type = response.headers.get_content_type()
        except (HTTPError, URLError, OSError, HTTPException):
            raise _BindingError("无法连接扫码服务，请检查网络或到 Server酱官网绑定。") from None
        if len(raw) > limit:
            raise _BindingError("扫码服务响应过大，请到 Server酱官网绑定。")
        return raw, media_type

    def _json(self, url: str, payload: dict | None = None) -> dict:
        raw, _ = self._request(url, payload=payload)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise _BindingError("扫码服务响应格式异常，请到 Server酱官网绑定。") from None
        if (
            not isinstance(result, dict)
            or type(result.get("code")) is not int
            or result["code"] != 0
            or not isinstance(result.get("data"), dict)
        ):
            raise _BindingError("扫码服务未确认请求，请重新扫码或到 Server酱官网绑定。")
        return result["data"]

    @staticmethod
    def _qr_url(data: dict) -> str:
        if not all(_text(data.get(field), minimum, maximum) for field, minimum, maximum in (
            ("ticket", 1, 1024), ("token", 16, 256), ("sid", 1, 128),
            ("url", 20, 1024), ("qr_url", 40, 2048),
        )):
            raise _BindingError("扫码服务返回的授权信息无效，请到 Server酱官网绑定。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", data["token"]):
            raise _BindingError("扫码服务返回的授权信息无效，请到 Server酱官网绑定。")
        try:
            qr = urlsplit(data["qr_url"])
            target = urlsplit(data["url"])
            query = parse_qs(qr.query, strict_parsing=True, max_num_fields=2)
        except ValueError:
            raise _BindingError("二维码地址格式无效，请到 Server酱官网绑定。") from None
        if (
            qr.scheme != "https" or qr.netloc != "mp.weixin.qq.com"
            or qr.path != "/cgi-bin/showqrcode" or qr.fragment
            or query != {"ticket": [data["ticket"]]}
            # This is the QR's WeChat scan target, never fetched by this app.
            # The official service currently returns an HTTP target while its
            # image URL and both API endpoints use HTTPS exclusively.
            or target.scheme not in ("http", "https") or target.netloc != "weixin.qq.com"
            or not re.fullmatch(r"/q/[A-Za-z0-9_-]{1,256}", target.path)
            or target.query or target.fragment
        ):
            raise _BindingError("扫码服务返回了非官方二维码地址，已停止绑定。")
        # Construct the single approved image URL rather than fetching an
        # arbitrary URL returned by another service.
        return "https://mp.weixin.qq.com/cgi-bin/showqrcode?" + urlencode({"ticket": data["ticket"]})

    def _image(self, url: str) -> str:
        raw, media_type = self._request(url, limit=_MAX_IMAGE)
        # WeChat's official showqrcode endpoint uses this legacy JPEG alias.
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33:
            width, height = struct.unpack(">II", raw[16:24])
            valid = raw[8:16] == b"\x00\x00\x00\rIHDR" and 1 <= width <= 2048 and 1 <= height <= 2048
        else:
            valid = media_type == "image/jpeg" and raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9")
        if not valid:
            raise _BindingError("二维码图片格式异常，已停止绑定；请到 Server酱官网扫码。")
        return f"data:{media_type};base64," + base64.b64encode(raw).decode("ascii")

    def start(self) -> dict:
        """Create one anonymous login session and download its official QR."""
        with self._lock:
            self._clear_session()
            started_at = time.monotonic()
            try:
                data = self._json(_SIGNIN)
                url = self._qr_url(data)
                duration = data.get("expire_seconds")
                if type(duration) is not int or not 0 < duration <= 86400:
                    raise _BindingError("二维码有效期异常，请到 Server酱官网绑定。")
                qr_image = self._image(url)
                self._token = data["token"]
                self._expires_at = started_at + min(duration, 600)
                self._qr_image = qr_image
                self._state = "waiting"
                self._message = "用手机微信扫码，按微信提示完成后点击“我已扫码，确认绑定”。不会自动发送测试消息。"
                self._expire()
            except _BindingError as error:
                self._clear_session()
                self._state = "error"
                self._message = str(error)
            return self.status()

    def confirm(self) -> str | None:
        """Check once at the user's request; never poll or send a message."""
        with self._lock:
            self._expire()
            if self._token is None:
                return None
            try:
                data = self._json(_CHECK, {"token": self._token})
                self._expire()
                if self._token is None:
                    return None
                if "result" not in data:
                    raise _BindingError("扫码服务未返回登录状态，请重新扫码或到官网绑定。")
                result = data.get("result")
                if result is None or result is False:
                    self._message = "尚未完成扫码，请按微信提示操作后再点击确认绑定。"
                    return None
                if not isinstance(result, dict) or _level(result.get("level")) is None:
                    raise _BindingError("扫码服务返回的登录状态无效，请重新扫码或到官网绑定。")
                if _level(result["level"]) == 0:
                    self._message = "尚未完成扫码，请按微信提示操作后再点击确认绑定。"
                    return None
                if "first_guide_done" in result and not _level(result["first_guide_done"]):
                    raise _BindingError(_FIRST_SETUP)
                try:
                    key = validate_sendkey(result.get("sendkey"))
                except NotificationError:
                    raise _BindingError(_FIRST_SETUP) from None
                self._clear_session()
                self._state = "bound"
                self._message = "扫码授权成功，未发送消息。保存成功后可点击发送测试，确认微信是否收到。"
                return key
            except _BindingError as error:
                self._clear_session()
                self._state = "error"
                self._message = str(error)
                return None
