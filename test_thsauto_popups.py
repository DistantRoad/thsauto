import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PopupCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只替换平台依赖，执行真实的窗口绑定和清理逻辑。
        with mock.patch.dict(sys.modules, {"pywinauto": mock.Mock(), "PIL": mock.Mock()}), \
                mock.patch("logging.getLogger"), \
                mock.patch("logging.handlers.RotatingFileHandler"):
            cls.module = _load_module("thsauto_popup_tests", "thsauto.py")

    def setUp(self):
        self.auto = object.__new__(self.module.ThsAuto)
        self.auto.app = mock.Mock()
        self.auto.main_window = mock.Mock(handle=1)
        self.auto.main_window.is_enabled.return_value = True
        self.auto.hwnd_main = 1
        self.auto._popup_windows = mock.Mock(return_value=[])
        self.hot_key = mock.patch.object(self.module, "hot_key").start()
        self.sleep = mock.patch.object(self.module.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def popup(handle):
        popup = mock.Mock(handle=handle)
        popup.is_visible.return_value = True
        popup.is_enabled.return_value = True
        return popup

    def test_no_popup_does_not_send_escape(self):
        self.assertEqual(0, self.auto.dismiss_popups())
        self.hot_key.assert_not_called()
        self.sleep.assert_not_called()

    def test_nested_popups_close_from_top_to_bottom(self):
        stack = [self.popup(2), self.popup(3), self.popup(4)]
        for popup in stack:
            popup.is_enabled.side_effect = lambda window=popup: bool(stack) and stack[-1] is window
        self.auto._popup_windows.side_effect = lambda: list(stack)
        self.auto.main_window.is_enabled.side_effect = lambda: not stack
        closed_handles = []

        def press_escape(keys):
            self.assertEqual(["esc"], keys)
            stack[-1].set_focus.assert_called_once_with()
            closed_handles.append(stack.pop().handle)

        self.hot_key.side_effect = press_escape

        self.assertEqual(3, self.auto.dismiss_popups())
        self.assertEqual([4, 3, 2], closed_handles)
        self.assertEqual(4, self.auto._popup_windows.call_count)

    def test_popup_disappearing_before_focus_is_rechecked(self):
        popup = self.popup(2)
        popup.set_focus.side_effect = RuntimeError("窗口已经关闭")
        self.auto._popup_windows.side_effect = [[popup], []]

        self.assertEqual(0, self.auto.dismiss_popups())
        self.hot_key.assert_not_called()
        self.assertEqual(2, self.auto._popup_windows.call_count)

    def test_waits_for_popup_when_main_window_is_disabled(self):
        popup = self.popup(2)
        self.auto._popup_windows.side_effect = [[], [popup], []]
        self.auto.main_window.is_enabled.side_effect = [False, True]

        self.assertEqual(1, self.auto.dismiss_popups())
        self.hot_key.assert_called_once_with(["esc"])
        self.assertEqual(2, self.sleep.call_count)

    def test_unclosable_popup_stops_at_retry_limit(self):
        popup = self.popup(2)
        self.auto._popup_windows.return_value = [popup]

        with self.assertRaisesRegex(RuntimeError, "尝试 3 次.*已停止执行命令"):
            self.auto.dismiss_popups(max_attempts=3)

        self.assertEqual(3, self.hot_key.call_count)
        self.assertEqual(4, self.auto._popup_windows.call_count)

    def test_disabled_main_window_without_popup_stops_at_retry_limit(self):
        self.auto.main_window.is_enabled.return_value = False

        with self.assertRaisesRegex(RuntimeError, "主窗口不可操作"):
            self.auto.dismiss_popups(max_attempts=2)

        self.hot_key.assert_not_called()
        self.assertEqual(2, self.sleep.call_count)

    def test_absent_client_skips_cleanup(self):
        self.auto.app = None
        self.auto.main_window = None
        self.auto.bind_client = mock.Mock(return_value=False)

        self.assertEqual(0, self.auto.dismiss_popups())
        self.auto.bind_client.assert_called_once_with(timeout=2, log_failure=False)
        self.auto._popup_windows.assert_not_called()
        self.hot_key.assert_not_called()

    def test_can_bind_main_window_disabled_by_modal_popup(self):
        main_window = self.auto.main_window
        main_window.is_enabled.return_value = False
        spec = mock.Mock()
        spec.wrapper_object.return_value = main_window
        application = mock.Mock()
        application.window.return_value = spec
        self.auto.active_main_window = mock.Mock()

        with mock.patch.object(self.module, "Application") as factory:
            factory.return_value.connect.return_value = application
            self.assertTrue(self.auto.bind_client())

        spec.wait.assert_called_once_with("exists", timeout=5)
        self.assertIs(main_window, self.auto.main_window)
        self.assertEqual(1, self.auto.hwnd_main)
        self.auto.active_main_window.assert_not_called()


class CommandCleanupTests(unittest.TestCase):
    def setUp(self):
        flask = mock.Mock()
        flask.Flask.return_value.config = {}
        flask.Flask.return_value.route.side_effect = lambda *args, **kwargs: lambda handler: handler
        thsauto = mock.Mock()
        with mock.patch.dict(sys.modules, {"flask": flask, "thsauto": thsauto}):
            self.server = _load_module("server_popup_tests", "server.py")
        self.server.interval = 0
        self.server.next_time = 0
        self.server.lock = threading.Lock()
        self.server.request = types.SimpleNamespace(path="/thsauto/balance", method="GET", args={})
        self.server.jsonify = lambda value: value

    def test_balance_and_position_clean_up_under_lock_before_running(self):
        events = []

        def cleanup():
            self.assertTrue(self.server.lock.locked())
            events.append("cleanup")

        self.server.auto.dismiss_popups.side_effect = cleanup
        self.server.auto.active_main_window.side_effect = lambda: events.append("focus")
        self.server.auto.get_balance.side_effect = lambda: events.append("balance") or {"balance": 100}
        self.server.auto.get_position.side_effect = lambda: events.append("position") or {"positions": []}

        self.assertEqual(({"balance": 100}, 200), self.server.get_balance())
        self.server.request.path = "/thsauto/position"
        self.assertEqual(({"positions": []}, 200), self.server.get_position())
        self.assertEqual(["cleanup", "focus", "balance", "cleanup", "focus", "position"], events)
        self.assertFalse(self.server.lock.locked())

    def test_cleanup_failure_blocks_command_and_releases_lock(self):
        self.server.auto.dismiss_popups.side_effect = RuntimeError("残留提示窗口无法关闭")

        with mock.patch.object(self.server.logging, "exception"), \
                mock.patch.object(self.server.traceback, "print_exc"):
            body, status = self.server.get_balance()

        self.assertEqual(400, status)
        self.assertEqual("failed", body["status"])
        self.assertIn("残留提示窗口无法关闭", body["msg"])
        self.server.auto.active_main_window.assert_not_called()
        self.server.auto.get_balance.assert_not_called()
        self.assertFalse(self.server.lock.locked())


if __name__ == "__main__":
    unittest.main()
