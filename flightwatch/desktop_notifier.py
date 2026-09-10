"""Token-free alerts through a locally logged-in Windows WeChat client.

Only the account's own 文件传输助手 is addressed. A successful receipt means
wxauto4 reported sending in the desktop client; it is not a phone-delivery
receipt. Each send runs in a short-lived child process so COM belongs to that
process and a timeout cannot leave an abandoned UI automation thread running.

Interfaces were checked against the 41.1.7 PyPI wheel's wx.pyi and param.py:
https://pypi.org/project/wxauto4/41.1.7/
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import threading
import uuid

from .notifier import NotificationError


RECIPIENT = "文件传输助手"
SUPPORTED_WXAUTO_VERSION = "41.1.7"
_ACK_PREFIX = "FLIGHTWATCH_WECHAT_RESULT:"
_SEND_LOCK = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_HELP = "请在 Windows 项目目录运行 python -m pip install -r requirements-wechat.txt。"


def _has_wechat_window() -> bool:
    """Inspect visible native windows without touching the WeChat UI."""
    win32gui = importlib.import_module("win32gui")
    found = []

    def inspect_window(hwnd, _):
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetWindowText(hwnd) == "微信"
            and win32gui.GetClassName(hwnd).startswith("Qt")
        ):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(inspect_window, None)
    return bool(found)


def desktop_status() -> dict:
    """Read-only prerequisites check; never imports wxauto4 or sends messages.

    ``available`` means a compatible dependency and visible WeChat window were
    found. Login is deliberately checked only immediately before a send because
    constructing the automation client can move or focus the WeChat window.
    """
    system = platform.system()
    result = {"available": False, "platform": system, "login_checked": False}
    if system != "Windows":
        result["message"] = (
            "免 token 微信推送需要在已登录微信的 Windows 电脑运行；"
            "当前系统仍可查询机票和设置监控。"
        )
        return result
    if platform.machine().upper() not in {"AMD64", "X86_64"} or not (3, 11) <= sys.version_info[:2] < (3, 14):
        result["message"] = "免 token 微信推送需要 Windows x64 和 Python 3.11–3.13。"
        return result
    try:
        for module in ("wxauto4", "pythoncom", "win32gui"):
            if importlib.util.find_spec(module) is None:
                result["message"] = "微信桌面推送依赖尚未安装。" + _INSTALL_HELP
                return result
        version = importlib.metadata.version("wxauto4")
        if version != SUPPORTED_WXAUTO_VERSION:
            result["message"] = "微信桌面推送依赖版本不匹配。" + _INSTALL_HELP
            return result
        if not _has_wechat_window():
            result["message"] = "未检测到微信窗口，请打开 Windows 微信 4.1.8，登录并保持窗口可见。"
            return result
    except Exception:
        result["message"] = "无法读取微信桌面环境。" + _INSTALL_HELP
        return result
    result.update(
        available=True,
        message="已检测到微信窗口和推送依赖；发送前会检查登录，仅发送到自己的文件传输助手。",
    )
    return result


class DesktopWeChatNotifier:
    """No credentials, arbitrary recipients, automatic retries or remote APIs."""

    def __init__(self, timeout: float = 20):
        try:
            valid = (
                not isinstance(timeout, bool)
                and isinstance(timeout, (int, float))
                and math.isfinite(timeout)
                and timeout > 0
            )
        except OverflowError:
            valid = False
        if not valid:
            raise NotificationError("微信桌面推送超时必须是有限正数。")
        self.timeout = float(timeout)

    def check_available(self) -> dict:
        return desktop_status()

    def send(self, title: str, content: str) -> str:
        if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
            raise NotificationError("推送标题必须为文本，推送内容不能为空。")
        try:
            payload = json.dumps({"title": title, "content": content}, ensure_ascii=False)
            payload.encode("utf-8")
        except UnicodeError:
            raise NotificationError("推送文本包含无效的 Unicode 字符。") from None
        status = self.check_available()
        if not status["available"]:
            raise NotificationError(status["message"])
        # Serialize complete UI interactions across request/monitor threads.
        if not _SEND_LOCK.acquire(timeout=self.timeout):
            raise NotificationError("上一条微信推送仍在处理，本次没有提交。")
        try:
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "flightwatch.desktop_notifier", "--worker"],
                    input=payload,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    cwd=str(_PROJECT_ROOT),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                raise NotificationError(
                    "微信桌面操作超时，已停止本次操作；发送结果未知，请查看文件传输助手。"
                ) from None
            except OSError:
                raise NotificationError("微信桌面推送进程无法启动。") from None
        finally:
            _SEND_LOCK.release()
        # The dependency may print informational text. Only our structured final
        # line is accepted; neither library output nor traceback is exposed.
        lines = [line for line in completed.stdout.splitlines() if line.startswith(_ACK_PREFIX)]
        try:
            ack = json.loads(lines[-1][len(_ACK_PREFIX):]) if lines else None
        except (ValueError, RecursionError):
            ack = None
        if completed.returncode or not isinstance(ack, dict):
            raise NotificationError("微信桌面操作未返回有效结果；请检查微信登录、版本和桌面状态。")
        if ack.get("ok") is not True:
            # Only our fixed worker messages can appear here; do not display
            # arbitrary subprocess data to the UI.
            code = ack.get("error")
            if not isinstance(code, str):
                code = "unknown"
            raise NotificationError(_WORKER_ERRORS.get(code, _WORKER_ERRORS["unknown"]))
        if ack.get("recipient") != RECIPIENT or ack.get("status") != "成功":
            raise NotificationError("微信桌面操作没有确认向文件传输助手发送。")
        return f"sent_to_client:{RECIPIENT}:{uuid.uuid4().hex}"


_WORKER_ERRORS = {
    "environment": "微信桌面推送环境不可用。" + _INSTALL_HELP,
    "offline": "微信尚未登录或网络离线，请打开并登录 Windows 微信。",
    "recipient": "未能精确打开自己的文件传输助手，本次未发送。",
    "rejected": "微信客户端未确认发送成功，请查看文件传输助手及微信连接状态。",
    "unknown": "微信桌面操作失败，发送结果未知；请检查微信版本、登录状态，并保持电脑桌面解锁。",
}


def _send_to_client(message: str) -> dict:
    """Called only in the isolated worker; never falls back to current chat."""
    if platform.system() != "Windows":
        return {"ok": False, "error": "environment"}
    pythoncom = None
    initialized = False
    try:
        pythoncom = importlib.import_module("pythoncom")
        pythoncom.CoInitialize()
        initialized = True
        wxauto = importlib.import_module("wxauto4")
        # Disable documented optional telemetry and file logging before client
        # construction. Flight data only belongs in the user's own self chat.
        wxauto.WxParam.TELEMETRY_ENABLED = False
        wxauto.WxParam.ENABLE_FILE_LOGGER = False
        wxauto.WxParam.LANGUAGE = "cn"
        client = wxauto.WeChat(debug=False, resize=False)
        if client.IsOnline() is not True:
            return {"ok": False, "error": "offline"}
        selected = client.ChatWith(who=RECIPIENT, exact=True, force=False)
        if selected is False or (isinstance(selected, Mapping) and selected.get("status") != "成功"):
            return {"ok": False, "error": "recipient"}
        chat = client.ChatInfo()
        if (
            not isinstance(chat, Mapping)
            or chat.get("chat_name") != RECIPIENT
            or chat.get("chat_type") in {"group", "service", "official"}
        ):
            return {"ok": False, "error": "recipient"}
        response = client.SendMsg(msg=message, who=RECIPIENT, clear=True, exact=True)
        # WxResponse subclasses dict. A nonempty failure dict is truthy in plain
        # Python, so require the exact documented status rather than bool(dict).
        if not isinstance(response, Mapping) or response.get("status") != "成功":
            return {"ok": False, "error": "rejected"}
        return {"ok": True, "recipient": RECIPIENT, "status": "成功"}
    except (ImportError, ModuleNotFoundError):
        return {"ok": False, "error": "environment"}
    except Exception:
        return {"ok": False, "error": "unknown"}
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _worker_main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        title, content = payload["title"], payload["content"]
        if not isinstance(title, str) or not isinstance(content, str) or not content.strip():
            raise ValueError("invalid message")
        result = _send_to_client(f"{title.strip()}\n\n{content}".strip())
    except Exception:
        result = {"ok": False, "error": "unknown"}
    print(_ACK_PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        raise SystemExit(_worker_main())
    print(json.dumps(desktop_status(), ensure_ascii=False, indent=2))
