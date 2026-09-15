"""Single-owner WeChat mini-program identity, subscriptions and message delivery.

Only the official API receives the AppSecret. Login establishes identity using
code2session; client-supplied OpenIDs are never accepted. No sends on setup/login.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .models import ConfigError
from .notifier import NotificationError, _NoRedirect


@dataclass(frozen=True)
class MiniConfig:
    appid: str
    secret: str
    template: str
    fields: dict
    state: str = "formal"

    @classmethod
    def from_env(cls):
        values = [os.environ.get(k, "").strip() for k in
                  ("MINIPROGRAM_APP_ID", "MINIPROGRAM_APP_SECRET", "MINIPROGRAM_TEMPLATE_ID")]
        if not all(values):
            raise ConfigError("请配置 MINIPROGRAM_APP_ID、MINIPROGRAM_APP_SECRET、MINIPROGRAM_TEMPLATE_ID")
        if not re.fullmatch(r"wx[a-fA-F0-9]{16}", values[0]):
            raise ConfigError("小程序 AppID 格式无效")
        try:
            fields = json.loads(os.environ.get("MINIPROGRAM_TEMPLATE_FIELDS", "{}"))
        except ValueError:
            raise ConfigError("MINIPROGRAM_TEMPLATE_FIELDS 必须为 JSON 对象") from None
        allowed = {"route": {"thing"}, "price": {"amount"}, "departure": {"date"},
                   "platform": {"thing"}, "flight": {"character_string", "thing"}}
        if not isinstance(fields, dict) or not 2 <= len(fields) <= 5:
            raise ConfigError("请按真实订阅模板配置 2～5 个字段；至少包括 route 和 price")
        for key, value in fields.items():
            match = re.fullmatch(r"(thing|amount|date|time|character_string)\d+", key)
            if not match or not isinstance(value, str) or match[1] not in allowed.get(value, set()):
                raise ConfigError("订阅模板字段类型与 route/price/departure/platform/flight 不匹配")
        if not {"route", "price"} <= set(fields.values()):
            raise ConfigError("订阅模板必须包含行程 route 和价格 price")
        state = os.environ.get("MINIPROGRAM_STATE", "formal")
        if state not in {"formal", "trial", "developer"}:
            raise ConfigError("MINIPROGRAM_STATE 必须为 formal、trial 或 developer")
        return cls(*values, fields, state)


class MiniStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "mini.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages
                    (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key, default=""):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def new_pairing(self):
        if self.get("owner"):
            raise ConfigError("已有绑定微信；不自动替换所有者")
        code = secrets.token_urlsafe(24)
        self.put("pair_hash", self.digest(code))
        self.put("pair_expires", time.time() + 86400)
        return code

    def login(self, openid, pairing=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            values = dict(db.execute("SELECT key,value FROM meta"))
            if values.get("owner"):
                if not hmac.compare_digest(values["owner"], openid):
                    raise ConfigError("这个服务已绑定其他微信，无法访问其行程")
            else:
                if (not values.get("pair_hash") or time.time() >= float(values.get("pair_expires", 0))
                        or not hmac.compare_digest(values["pair_hash"], self.digest(pairing))):
                    raise ConfigError("首次绑定需要服务器生成的有效绑定码")
                db.execute("INSERT INTO meta VALUES ('owner',?)", (openid,))
                db.execute("DELETE FROM meta WHERE key IN ('pair_hash','pair_expires')")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?)", (self.digest(token), time.time() + 7 * 86400))
            # Keep a bounded number of active devices/sessions.
            db.execute("DELETE FROM sessions WHERE hash NOT IN (SELECT hash FROM sessions ORDER BY expires DESC LIMIT 20)")
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 100:
            return False
        with self.db() as db:
            return db.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                              (self.digest(token), time.time())).fetchone() is not None

    def message(self, identifier):
        with self.db() as db:
            row = db.execute("SELECT payload FROM messages WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_message(self, identifier, payload):
        with self.db() as db:
            db.execute("DELETE FROM messages WHERE created<?", (time.time() - 90 * 86400,))
            db.execute("INSERT INTO messages VALUES (?,?,?)", (identifier, json.dumps(payload), time.time()))


class WeChatMiniAPI:
    def __init__(self, config: MiniConfig):
        self.config = config
        self.opener = build_opener(_NoRedirect())
        self._lock = threading.Lock()
        self._token, self._expires = "", 0

    def request(self, path, *, params=None, body=None):
        url = "https://api.weixin.qq.com" + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            # Never expose AppSecret, access_token, code or raw upstream errors.
            raise NotificationError("微信接口未返回有效响应，请稍后重试") from None

    def openid(self, code):
        result = self.request("/sns/jscode2session", params={
            "appid": self.config.appid, "secret": self.config.secret,
            "js_code": code, "grant_type": "authorization_code"})
        value = result.get("openid")
        if result.get("errcode", 0) != 0 or not isinstance(value, str) or not re.fullmatch(r"[\w-]{10,128}", value, re.ASCII):
            raise NotificationError("微信登录失败，请重新点击登录")
        return value

    def token(self):
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            result = self.request("/cgi-bin/stable_token", body={
                "grant_type": "client_credential", "appid": self.config.appid,
                "secret": self.config.secret, "force_refresh": False})
            token, ttl = result.get("access_token"), result.get("expires_in")
            if not isinstance(token, str) or not token or type(ttl) is not int or ttl <= 60:
                raise NotificationError("微信凭证获取失败，请核对服务器 AppID、AppSecret 和 IP 白名单")
            self._token, self._expires = token, time.monotonic() + ttl - 60
            return token


class MiniNotifier:
    single_route_messages = True

    def __init__(self, api: WeChatMiniAPI, store: MiniStore):
        self.api, self.store = api, store

    def send_quote(self, title, content, route, quote):
        owner = self.store.get("owner")
        if not owner or self.store.get("subscription") != "accepted":
            raise NotificationError("请在小程序点击订阅提醒，微信授权次数可能已用完")
        if not quote.comparable or quote.currency != "CNY":
            raise NotificationError("未核实总价的机票不能发送降价提醒")
        config = self.api.config
        values = {"route": f"{route.origin}→{route.destination}", "price": f"¥{quote.price:.2f}",
                  "departure": quote.departure_date.isoformat(), "platform": quote.source,
                  "flight": quote.flight_number or "待确认"}
        data = {}
        for key, field in config.fields.items():
            value = values[field]
            if key.startswith("thing"):
                value = value[:20]
            elif key.startswith("character_string"):
                value = re.sub(r"[^a-zA-Z0-9_\-]", "", value)[:32] or "NA"
            data[key] = {"value": value}
        identifier = secrets.token_hex(16)
        # Persist before send: a promptly opened card must already resolve.
        self.store.save_message(identifier, {"title": title, "content": content,
            "route_id": route.id, "date": quote.departure_date.isoformat(),
            "price": str(quote.price), "platform": quote.source, "url": quote.url})
        result = self.api.request("/cgi-bin/message/subscribe/send",
            params={"access_token": self.api.token()}, body={"touser": owner,
                "template_id": config.template, "page": "pages/detail/index?id=" + identifier,
                "miniprogram_state": config.state, "lang": "zh_CN", "data": data})
        code = result.get("errcode")
        if type(code) is not int or code != 0:
            if code == 43101:
                self.store.put("subscription", "needed")
                raise NotificationError("微信订阅授权不可用或次数已用完，请在小程序重新订阅；未记为已提醒")
            if code in (40001, 40014, 42001):
                self.api._expires = 0
            raise NotificationError(f"微信未受理订阅提醒（错误码 {code if type(code) is int else '未知'}）；未记为已提醒")
        return "accepted:mini:" + identifier
