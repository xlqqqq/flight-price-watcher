"""Local ServerChan credential storage; public status never returns the key."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .notifier import NotificationError


FILENAME = "serverchan-secret.json"


def read_sendkey(data_dir: Path) -> str:
    from .serverchan_notifier import validate_sendkey
    target = Path(data_dir) / FILENAME
    try:
        if target.is_symlink():
            raise NotificationError("微信服务号凭证文件不能是符号链接，请重新保存。")
        with target.open("rb") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"sendkey"}:
            raise ValueError
        return validate_sendkey(data["sendkey"])
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, TypeError, KeyError, RecursionError, NotificationError):
        raise NotificationError("无法读取微信服务号配置，请重新保存有效的 SendKey。") from None


def save_sendkey(data_dir: Path, value: str) -> None:
    from .serverchan_notifier import validate_sendkey
    value = validate_sendkey(value)
    data_dir = Path(data_dir)
    temporary = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".serverchan-", suffix=".tmp", dir=data_dir)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"sendkey": value}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_dir / FILENAME)
    except OSError:
        raise NotificationError("无法保存微信服务号配置，请检查 data 目录权限。") from None
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def clear_sendkey(data_dir: Path) -> None:
    try:
        (Path(data_dir) / FILENAME).unlink(missing_ok=True)
    except OSError:
        raise NotificationError("无法清除微信服务号配置，请检查 data 目录权限。") from None


def channel_status(data_dir: Path) -> dict:
    from .serverchan_notifier import quota_status
    try:
        key = read_sendkey(data_dir)
        if not key:
            return dict(configured=False, available=False, quota=None,
                        message="使用手机微信扫码并确认绑定，程序自动保存推送凭证；无需桌面微信或定期互动，每天最多提交 5 次。")
        quota = quota_status(key, data_dir)
        return dict(configured=True, available=True, quota=quota,
                    message="已保存推送密钥，尚需在微信确认实际收到；本程序每天最多提交 5 次，测试及失败也计数。")
    except NotificationError as exc:
        return dict(configured=False, available=False, quota=None, message=str(exc))


def make_notifier(data_dir: Path, timeout: float = 20):
    from .serverchan_notifier import ServerChanNotifier
    return ServerChanNotifier(read_sendkey(data_dir), data_dir, timeout=timeout)
