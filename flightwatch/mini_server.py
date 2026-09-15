"""Dedicated authenticated mini-program backend; keep the desktop port private."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import load_env
from .miniprogram import MiniConfig, MiniNotifier, MiniStore, WeChatMiniAPI
from .models import ConfigError, ProviderError
from .notifier import NotificationError
from .webapp import Dashboard, normalize_request

ROOT = Path(__file__).resolve().parents[1]


class MiniService:
    def __init__(self, config, directory):
        self.store = MiniStore(directory)
        self.api = WeChatMiniAPI(config)
        self.dashboard = Dashboard(directory, alert_notifier=MiniNotifier(self.api, self.store))
        self.control = threading.Lock()
        self.rate_lock = threading.Lock()
        self.rates = {}

    def allow(self, login=False):
        # One personal owner; a global bucket also bounds unauthenticated work.
        key = "login" if login else "api"
        with self.rate_lock:
            start, count = self.rates.get(key, (time.monotonic(), 0))
            if time.monotonic() - start >= 60:
                start, count = time.monotonic(), 0
            self.rates[key] = (start, count + 1)
            return count < (10 if login else 180)

    def bootstrap(self):
        boot = self.dashboard.bootstrap()
        return {**{key: boot[key] for key in ("today", "max_date", "defaults", "providers", "version")},
                "template_id": self.api.config.template,
                "subscription": self.store.get("subscription", "needed"),
                "subscription_note": "一次性订阅通常每次授权可发送一条消息；多行程分别发送。长期订阅以微信后台实际开放的模板为准。"}

    def status(self):
        status = self.dashboard.status()
        return {**{key: status[key] for key in ("latest", "monitor", "notifications", "search_busy")},
                "subscription": self.store.get("subscription", "needed")}

    def action(self, action, raw):
        with self.control:
            if action == "subscribe":
                if set(raw) != {"template_id", "result"} or raw["template_id"] != self.api.config.template:
                    raise ConfigError("订阅模板不匹配")
                if raw["result"] not in {"accept", "reject", "ban"}:
                    raise ConfigError("订阅操作结果无效")
                # This is only a client hint. WeChat remains authoritative;
                # never invent a send quota from a client-reported accept.
                self.store.put("subscription", "accepted" if raw["result"] == "accept" else "needed")
                return
            if action == "stop":
                self.store.put("monitor_enabled", "0")
                self.dashboard.stop()
                return
            # Notification routing is owned by this server, not client input.
            form = normalize_request({**raw, "notify": "browser"})
            if action == "search":
                self.dashboard.search(form)
            elif action == "start":
                if self.store.get("subscription") != "accepted":
                    raise ConfigError("请先点击订阅提醒，再开始监控")
                self.dashboard.start(form)
                self.store.put("monitor_enabled", "1")
            elif action == "save":
                if self.dashboard.status()["monitor"]["running"]:
                    raise ConfigError("请先停止监控，再保存修改后的行程")
                path = self.dashboard.data_dir / "web-settings.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(form, ensure_ascii=False))
                temporary.replace(path)
            else:
                raise ConfigError("未知操作")

    def resume(self):
        if self.store.get("monitor_enabled") != "1":
            return
        try:
            raw = json.loads((self.dashboard.data_dir / "web-settings.json").read_text())
            self.dashboard.start({**raw, "notify": "browser"})
        except (OSError, ValueError, ConfigError) as exc:
            with self.dashboard.lock:
                self.dashboard.monitor["last_error"] = (str(exc) if isinstance(exc, ConfigError)
                                                        else "已保存监控无法恢复，请在小程序重新设置")


class MiniServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, MiniHandler)


class MiniHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_args):
        pass  # Avoid logging login codes, session tokens or private route URLs.

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def dispatch(self, post=False):
        service = self.server.service
        parsed = urlsplit(self.path)
        path = parsed.path
        if not service.allow(login=path == "/api/login"):
            return self.reply(429, {"error": "操作太频繁，请一分钟后重试"})
        if not post and path == "/health":
            return self.reply(200, {"ok": True, "version": __version__})
        if path != "/api/login":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not service.store.authenticate(auth[7:]):
                return self.reply(401, {"error": "请先使用微信登录"})
        raw = {}
        if post:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                return self.reply(400, {"error": "请求格式或大小无效"})
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict):
                return self.reply(400, {"error": "请求须为 JSON 对象"})
        if post and path == "/api/login":
            if (set(raw) - {"code", "pairing"} or not isinstance(raw.get("code"), str)
                    or not 1 <= len(raw["code"]) <= 256 or not isinstance(raw.get("pairing", ""), str)
                    or len(raw.get("pairing", "")) > 128):
                raise ConfigError("登录参数无效")
            owner = service.api.openid(raw["code"])
            token = service.store.login(owner, raw.get("pairing", ""))
            return self.reply(200, {"token": token})
        if not post and path == "/api/bootstrap":
            return self.reply(200, service.bootstrap())
        if not post and path == "/api/status":
            return self.reply(200, service.status())
        if not post and path == "/api/cities":
            query = parse_qs(parsed.query, max_num_fields=1).get("q", [""])[0]
            return self.reply(200, service.dashboard.city_lookup(query))
        if not post and path.startswith("/api/messages/"):
            message = service.store.message(path.rsplit("/", 1)[-1])
            return self.reply(200 if message else 404, message or {"error": "提醒记录不存在或已过期"})
        if post and path in {"/api/search", "/api/start", "/api/stop", "/api/save", "/api/subscribe"}:
            service.action(path.rsplit("/", 1)[-1], raw)
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "接口不存在"})

    def handle_request(self, post=False):
        try:
            self.dispatch(post)
        except (ConfigError, NotificationError, ProviderError) as exc:
            self.reply(400, {"error": str(exc)})
        except (ValueError, TypeError, RecursionError):
            self.reply(400, {"error": "请求内容无效"})
        except Exception:
            self.reply(500, {"error": "服务处理失败，请检查服务器运行状态"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


def main():
    parser = argparse.ArgumentParser(description="微信小程序专用后端（单人、多行程）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/miniprogram")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pair", action="store_true", help="生成首次绑定码，24 小时有效；不发送消息")
    mode.add_argument("--check", action="store_true", help="检查配置，不联网、不发送消息")
    args = parser.parse_args()
    try:
        if args.pair:
            print("首次绑定码（请勿公开）：" + MiniStore(args.data_dir).new_pairing())
            return 0
        load_env(args.env)
        config = MiniConfig.from_env()
        if args.check:
            print("配置格式检查通过；仍须真机确认模板授权、域名及微信实际送达")
            return 0
        service = MiniService(config, args.data_dir)
        from .cli import process_lock
        with process_lock(args.data_dir / "server.lock"):
            server = MiniServer(("127.0.0.1", args.port), service)
            service.resume()
            print(f"小程序后端：http://127.0.0.1:{args.port}；上线通过独立 HTTPS 域名反向代理")
            try:
                server.serve_forever()
            finally:
                service.dashboard.stop()
                server.server_close()
        return 0
    except (ConfigError, NotificationError) as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
