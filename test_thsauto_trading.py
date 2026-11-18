import ast
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent


def _load_thsauto_module():
    spec = importlib.util.spec_from_file_location("thsauto_trading_tests", ROOT / "thsauto.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TradingCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(sys.modules, {"pywinauto": mock.Mock(), "PIL": mock.Mock()}), \
                mock.patch("logging.getLogger"), \
                mock.patch("logging.handlers.RotatingFileHandler"):
            cls.mod = _load_thsauto_module()

    def setUp(self):
        self.auto = object.__new__(self.mod.ThsAuto)
        self.auto.app = mock.Mock()
        self.auto.main_window = mock.Mock(handle=1)
        self.auto.main_window.wrapper_object.return_value = self.auto.main_window
        self.auto.main_window.is_enabled.return_value = True
        self.auto.hwnd_main = 1
        self.auto._popup_windows = mock.Mock(return_value=[])
        self.hot_key = mock.patch.object(self.mod, "hot_key").start()
        self.sleep = mock.patch.object(self.mod.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_python38_syntax_compatibility(self):
        for filename in ("thsauto.py", "server.py", "llm_ocr.py", "start_server.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            ast.parse(source, filename=filename, feature_version=(3, 8))

    def test_parse_table_filters_ths_empty_placeholder_and_summary_rows(self):
        # 模拟同花顺 F8 无委托时复制出的 2 行空占位行
        empty_orders_raw = (
            "委托时间\t证券代码\t证券名称\t操作\t备注\t委托数量\t成交数量\t委托价格\t成交均价\t撤单数量\t合同编号\t交易市场\t\r\n"
            "\t\t\t\t\t0\t0\t0.000\t0.000\t0\t\t\t\r\n"
            "\t\t\t\t\t0\t0\t0.000\t0.000\t0\t\t\t"
        )
        self.assertEqual([], self.mod.parse_table(empty_orders_raw))

        # 模拟持仓表含真实股票 + 逆回购 + 底部汇总行
        positions_raw = (
            "操作\t证券代码\t证券名称\t股票余额\t市值\t\r\n"
            "\t600757\t长江传媒\t6000\t52260.000\t\r\n"
            "\t131990\t131990\t0\t0.000\t\r\n"
            "汇总\t\t\t\t1351290.000\t"
        )
        parsed = self.mod.parse_table(positions_raw)
        self.assertEqual(2, len(parsed))
        self.assertEqual(["600757", "131990"], [r["证券代码"] for r in parsed])
        self.assertNotIn("", parsed[0])

        # 模拟不含证券代码的通用流水表不应误删有效行
        transfer_raw = (
            "发生日期\t业务名称\t发生金额\t备注\t\r\n"
            "20261118\t银行转证券\t10000.00\t成功\t"
        )
        self.assertEqual(
            [{"发生日期": "20261118", "业务名称": "银行转证券", "发生金额": "10000.00", "备注": "成功"}],
            self.mod.parse_table(transfer_raw),
        )

    def test_format_trade_price_handles_stocks_and_etfs_without_float_artifacts(self):
        fmt = self.mod.format_trade_price
        self.assertEqual("7.90", fmt(7.9))
        self.assertEqual("7.91", fmt(7.91))
        self.assertEqual("7.91", fmt("7.910"))
        self.assertEqual("10.00", fmt(10))
        self.assertEqual("1.234", fmt(1.234))
        self.assertEqual("100.005", fmt("100.005"))
        with self.assertRaises(ValueError):
            fmt(0)
        with self.assertRaises(ValueError):
            fmt(-5.2)
        with self.assertRaises(ValueError):
            fmt("abc")

    def test_trade_field_matches_numeric_equivalence(self):
        match = self.mod.ThsAuto._trade_field_matches
        self.assertTrue(match("7.91", "7.910", "买卖价格"))
        self.assertTrue(match("7.910", "7.91", "买卖价格"))
        self.assertFalse(match("7.91", "7.92", "买卖价格"))
        self.assertFalse(match("000001", "1", "证券代码"))

    def test_submit_trade_result_does_not_spam_y_without_confirm_popup(self):
        self.auto.get_result = mock.Mock(
            side_effect=[
                None,
                None,
                {"code": 0, "status": "succeed", "msg": "您的买入委托已成功提交，合同编号：5500。", "entrust_no": "5500"},
            ]
        )
        self.auto._find_trade_confirm_popup = mock.Mock(return_value=(None, None))

        res = self.auto._submit_trade_result("买入成功", "买入失败")
        self.assertEqual(0, res["code"])
        self.assertEqual("5500", res["entrust_no"])
        # 只发送了一次初始 Enter，没有盲目发送任何 y 键
        self.assertEqual([mock.call(["enter"])], self.hot_key.call_args_list)

    def test_submit_trade_result_clicks_yes_button_when_confirm_popup_present(self):
        popup = mock.Mock(handle=99)
        yes_btn = mock.Mock()
        self.auto._find_trade_confirm_popup = mock.Mock(
            side_effect=[(popup, yes_btn), (None, None)]
        )
        self.auto.get_result = mock.Mock(
            side_effect=[
                None,
                {"code": 0, "status": "succeed", "msg": "您的买入委托已成功提交，合同编号：5501。", "entrust_no": "5501"},
            ]
        )

        res = self.auto._submit_trade_result("买入成功", "买入失败")
        self.assertEqual("5501", res["entrust_no"])
        yes_btn.click_input.assert_called_once_with()

    def test_cancel_uses_keyboard_row_selection_and_supports_no_confirm_mode(self):
        grid = mock.Mock(handle=200)
        self.auto.switch_to_normal = mock.Mock(return_value=False)
        self.auto.refresh = mock.Mock()
        self.auto._get_right_control = mock.Mock(return_value=grid)
        table_raw = (
            "委托时间\t证券代码\t合同编号\t\r\n"
            "12:30:01\t600757\t5461\t\r\n"
            "12:30:00\t600757\t5460\t\r\n"
            "12:29:59\t600757\t5459\t"
        )
        self.auto._wait_clipboard_table = mock.Mock(return_value=table_raw)
        self.auto._is_cancel_no_confirm_enabled = mock.Mock(return_value=True)
        self.auto.get_result = mock.Mock(return_value=None)
        self.auto._find_cancel_confirm_popup = mock.Mock(return_value=None)

        res = self.auto.cancel("5460")
        self.assertEqual({"code": 0, "status": "succeed"}, res)
        grid.double_click_input.assert_not_called()
        sent_keys = [c.args[0] for c in self.hot_key.call_args_list]
        self.assertEqual(
            [["F3"], ["ctrl", "home"], ["pgup"], ["home"], ["down"], ["enter"]],
            sent_keys,
        )

    def test_cancel_aborts_if_confirm_popup_shows_mismatched_entrust_no(self):
        popup = mock.Mock(handle=301)
        self.auto._is_cancel_no_confirm_enabled = mock.Mock(return_value=False)
        self.auto.get_result = mock.Mock(return_value=None)
        self.auto._find_cancel_confirm_popup = mock.Mock(return_value=popup)
        self.auto._window_static_texts = mock.Mock(return_value=["撤单确认 合同编号：9999"])
        self.auto._dismiss_stubborn_popup = mock.Mock()

        res = self.auto._submit_cancel_result(entrust_no="5460")
        self.assertEqual(1, res["code"])
        self.assertIn("不一致", res["msg"])
        self.auto._dismiss_stubborn_popup.assert_called_once_with(popup)

    def test_get_balance_handles_commas_and_placeholder_dashes(self):
        self.auto.switch_to_normal = mock.Mock(return_value=False)
        self.auto.refresh = mock.Mock()

        values = {
            0x3F7: "1,394,320.87",  # 总资产
            0x3F6: "1,351,290.00",  # 股票市值
            0x3F5: "43,001.42",     # 冻结金额
        }

        def fake_right_ctrl(cid, class_name=None, timeout=1.5):
            if cid not in values:
                raise RuntimeError("not found")
            ctrl = mock.Mock()
            ctrl.wrapper_object.return_value = ctrl
            ctrl.window_text.return_value = values[cid]
            return ctrl

        self.auto._get_right_control = mock.Mock(side_effect=fake_right_ctrl)
        res = self.auto.get_balance()
        self.assertEqual("29.45", res["data"]["可用金额"])

        # 若某字段为 '--'，不应抛异常崩溃
        values[0x3F5] = "--"
        res2 = self.auto.get_balance()
        self.assertEqual(0, res2["code"])
        self.assertNotIn("可用金额", res2["data"])

    def test_switch_to_normal_and_kc_do_not_click_right_panel_tabs(self):
        self.auto.active_main_window = mock.Mock()
        right_panel = mock.Mock()
        right_panel.rectangle.return_value = mock.Mock(left=400)
        self.auto.get_right_hwnd = mock.Mock(return_value=right_panel)

        # 右侧中部的 持仓/成交/委托 标签栏 (left=585 >= right_panel.left=400)
        right_tab = mock.Mock(handle=88)
        right_tab.is_visible.return_value = True
        right_tab.control_id.return_value = 0x54B
        right_tab.rectangle.return_value = mock.Mock(left=585, right=785, top=401, bottom=423)
        self.auto.main_window.descendants.return_value = [right_tab]

        self.assertFalse(self.auto.switch_to_normal())
        right_tab.click_input.assert_not_called()

        self.auto._fill_trade_form = mock.Mock()
        self.auto._submit_trade_result = mock.Mock(return_value={"code": 0, "status": "succeed"})
        self.auto.buy_kc("688001", 200, 50.5)
        right_tab.click_input.assert_not_called()

    def test_dismiss_popups_closes_yes_no_dialog_ignoring_esc(self):
        popup = mock.Mock(handle=330372)
        popup.is_visible.return_value = True
        popup.is_enabled.return_value = True
        popup.style.return_value = 0x00C00000
        popup.exstyle.return_value = 0

        no_btn = mock.Mock()
        no_btn.wrapper_object.return_value = no_btn
        no_btn.is_visible.return_value = True
        no_btn.is_enabled.return_value = True
        no_btn.control_id.return_value = 7
        no_btn.window_text.return_value = "否(&N)"
        popup.descendants.return_value = [no_btn]

        active = [popup]
        no_btn.click_input.side_effect = lambda: active.clear()
        self.auto._popup_windows.side_effect = lambda: list(active)

        count = self.auto.dismiss_popups(max_attempts=5)
        self.assertEqual(2, count)
        no_btn.click_input.assert_called_once_with()
        self.assertEqual([], active)


if __name__ == "__main__":
    unittest.main()
