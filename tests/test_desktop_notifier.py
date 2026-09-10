"""Contract tests using fake Windows and wxauto clients; no live sends."""

import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flightwatch.desktop_notifier import (
    DesktopWeChatNotifier,
    RECIPIENT,
    _ACK_PREFIX,
    _send_to_client,
    desktop_status,
)
from flightwatch.notifier import NotificationError


class DesktopStatusTests(unittest.TestCase):
    def test_linux_fails_clearly_without_importing_windows_dependencies(self):
        with patch("flightwatch.desktop_notifier.platform.system", return_value="Linux"), patch(
            "flightwatch.desktop_notifier.importlib.import_module"
        ) as load:
            status = desktop_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["platform"], "Linux")
        self.assertIn("Windows", status["message"])
        load.assert_not_called()

    def set_up_windows(self):
        for name, value in (
            ("platform.system", "Windows"),
            ("platform.machine", "AMD64"),
            ("importlib.util.find_spec", object()),
            ("importlib.metadata.version", "41.1.7"),
            ("_has_wechat_window", True),
        ):
            mocker = patch("flightwatch.desktop_notifier." + name, return_value=value)
            self.addCleanup(mocker.stop)
            mocker.start()
        version = patch("flightwatch.desktop_notifier.sys.version_info", (3, 11, 0))
        self.addCleanup(version.stop)
        version.start()

    def test_available_checks_never_construct_a_wechat_client(self):
        self.set_up_windows()
        with patch("flightwatch.desktop_notifier.importlib.import_module") as load:
            status = desktop_status()
        self.assertTrue(status["available"])
        self.assertFalse(status["login_checked"])
        self.assertIn("发送前", status["message"])
        load.assert_not_called()

    def test_missing_dependency_reports_install_command(self):
        self.set_up_windows()
        with patch("flightwatch.desktop_notifier.importlib.util.find_spec", return_value=None):
            status = desktop_status()
        self.assertFalse(status["available"])
        self.assertIn("requirements-wechat.txt", status["message"])

    def test_incompatible_version_and_no_window_fail(self):
        self.set_up_windows()
        with patch("flightwatch.desktop_notifier.importlib.metadata.version", return_value="40.1.1"):
            self.assertFalse(desktop_status()["available"])
        with patch("flightwatch.desktop_notifier._has_wechat_window", return_value=False):
            self.assertIn("未检测到微信窗口", desktop_status()["message"])


class DesktopTransportTests(unittest.TestCase):
    def setUp(self):
        available = patch("flightwatch.desktop_notifier.desktop_status", return_value={"available": True})
        available.start()
        self.addCleanup(available.stop)
        runner = patch("flightwatch.desktop_notifier.subprocess.run")
        self.run = runner.start()
        self.addCleanup(runner.stop)
        self.notifier = DesktopWeChatNotifier()

    def respond(self, ack, returncode=0):
        self.run.return_value = SimpleNamespace(
            stdout="dependency informational text\n" + _ACK_PREFIX + json.dumps(ack),
            returncode=returncode,
        )

    def test_acknowledges_only_confirmed_desktop_submission(self):
        self.respond({"ok": True, "recipient": RECIPIENT, "status": "成功"})
        receipt = self.notifier.send("低价机票", "上海 → 东京 699 元")
        self.assertTrue(receipt.startswith("sent_to_client:文件传输助手:"))
        self.run.assert_called_once()
        kwargs = self.run.call_args.kwargs
        self.assertEqual(json.loads(kwargs["input"]), {"title": "低价机票", "content": "上海 → 东京 699 元"})
        self.assertEqual(kwargs["timeout"], 20)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(self.run.call_args.args[0][-3:], ["-m", "flightwatch.desktop_notifier", "--worker"])

    def test_error_acks_and_wrong_recipient_never_become_success(self):
        for ack in (
            {"ok": False, "error": "rejected"},
            {"ok": True, "recipient": "某个朋友", "status": "成功"},
            {"ok": True, "recipient": RECIPIENT, "status": "失败"},
            {"ok": "true", "recipient": RECIPIENT, "status": "成功"},
            {"ok": False, "error": {"unsafe": "text"}},
            [],
        ):
            with self.subTest(ack=ack), self.assertRaises(NotificationError):
                self.respond(ack)
                self.notifier.send("标题", "内容")

    def test_nonzero_exit_and_unstructured_output_fail(self):
        self.respond({"ok": True, "recipient": RECIPIENT, "status": "成功"}, returncode=1)
        with self.assertRaises(NotificationError):
            self.notifier.send("标题", "内容")
        self.run.return_value = SimpleNamespace(stdout="unsafe library exception", returncode=0)
        with self.assertRaises(NotificationError) as caught:
            self.notifier.send("标题", "内容")
        self.assertNotIn("unsafe", str(caught.exception))

    def test_timeout_reports_unknown_without_retry(self):
        self.run.side_effect = subprocess.TimeoutExpired(["python"], 20)
        with self.assertRaisesRegex(NotificationError, "发送结果未知"):
            self.notifier.send("标题", "内容")
        self.run.assert_called_once()

    def test_invalid_input_does_not_start_worker(self):
        for timeout in (True, "20", 0, -1, float("inf"), float("nan"), 10**1000):
            with self.subTest(timeout_type=type(timeout).__name__), self.assertRaises(NotificationError):
                DesktopWeChatNotifier(timeout)
        for title, content in ((None, "内容"), ("标题", " "), ("标题", None), ("标题", "\ud800")):
            with self.subTest(title=title), self.assertRaises(NotificationError):
                self.notifier.send(title, content)
        self.run.assert_not_called()

    def test_unavailable_environment_does_not_start_worker(self):
        with patch("flightwatch.desktop_notifier.desktop_status", return_value={"available": False, "message": "需要 Windows"}):
            with self.assertRaisesRegex(NotificationError, "需要 Windows"):
                self.notifier.send("标题", "内容")
        self.run.assert_not_called()


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.IsOnline.return_value = True
        self.client.ChatWith.return_value = None
        self.client.ChatInfo.return_value = {"chat_name": RECIPIENT, "chat_type": "friend"}
        self.client.SendMsg.return_value = {"status": "成功", "message": None, "data": None}
        self.com = Mock()
        self.wxauto = SimpleNamespace(WxParam=SimpleNamespace(), WeChat=Mock(return_value=self.client))
        modules = {"pythoncom": self.com, "wxauto4": self.wxauto}
        windows = patch("flightwatch.desktop_notifier.platform.system", return_value="Windows")
        windows.start()
        self.addCleanup(windows.stop)
        loader = patch("flightwatch.desktop_notifier.importlib.import_module", side_effect=modules.__getitem__)
        loader.start()
        self.addCleanup(loader.stop)

    def test_only_exact_self_chat_receives_message_and_com_is_scoped(self):
        result = _send_to_client("航班低价消息")
        self.assertEqual(result, {"ok": True, "recipient": RECIPIENT, "status": "成功"})
        self.client.ChatWith.assert_called_once_with(who=RECIPIENT, exact=True, force=False)
        self.client.SendMsg.assert_called_once_with(msg="航班低价消息", who=RECIPIENT, clear=True, exact=True)
        self.com.CoInitialize.assert_called_once()
        self.com.CoUninitialize.assert_called_once()
        self.assertFalse(self.wxauto.WxParam.TELEMETRY_ENABLED)

    def test_truthy_rejected_response_does_not_mark_success(self):
        for response in ({"status": "失败"}, {"status": "错误"}, {"message": "success"}, True, None, "成功"):
            with self.subTest(response=response):
                self.client.SendMsg.return_value = response
                self.assertEqual(_send_to_client("内容"), {"ok": False, "error": "rejected"})

    def test_wrong_chat_or_same_named_group_never_sends(self):
        for info in ({"chat_name": "另一个联系人"}, {"chat_name": RECIPIENT, "chat_type": "group"}, {}, None):
            with self.subTest(info=info):
                self.client.ChatInfo.return_value = info
                self.assertEqual(_send_to_client("内容"), {"ok": False, "error": "recipient"})
        self.client.SendMsg.assert_not_called()

    def test_failed_chat_selection_has_no_fallback(self):
        self.client.ChatWith.return_value = {"status": "失败"}
        self.assertEqual(_send_to_client("内容"), {"ok": False, "error": "recipient"})
        self.client.SendMsg.assert_not_called()

    def test_offline_client_never_selects_or_sends(self):
        self.client.IsOnline.return_value = False
        self.assertEqual(_send_to_client("内容"), {"ok": False, "error": "offline"})
        self.client.ChatWith.assert_not_called()
        self.client.SendMsg.assert_not_called()

    def test_unexpected_exception_is_sanitized_and_com_released(self):
        self.client.SendMsg.side_effect = RuntimeError("private chat content")
        self.assertEqual(_send_to_client("内容"), {"ok": False, "error": "unknown"})
        self.com.CoUninitialize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
