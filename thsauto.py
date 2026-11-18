import ctypes
import json
import logging
import os
import re
import subprocess
import time
from base64 import b64encode
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from logging.handlers import RotatingFileHandler
from typing import Iterable

from pywinauto import Application, clipboard, keyboard, mouse
from PIL import ImageGrab

from const import BALANCE_CONTROL_ID_GROUP
import llm_ocr

LOG_FILE_PATH = os.path.abspath(os.getenv("THSAUTO_LOG_FILE", "thsauto.log"))
# 日志按大小轮转：超过上限后依次改名为 thsauto.log.1、thsauto.log.2……，最多保留 LOG_BACKUP_COUNT 个旧文件。
# 旧文件个数至少为1，否则 RotatingFileHandler 无法截断当前文件，日志会无限增长。
LOG_MAX_BYTES = int(os.getenv("THSAUTO_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = max(1, int(os.getenv("THSAUTO_LOG_BACKUP_COUNT", "10")))


def _setup_logging():
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    has_stream = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    )
    if not has_stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    has_file = any(
        isinstance(handler, logging.FileHandler)
        and os.path.abspath(getattr(handler, "baseFilename", "")) == LOG_FILE_PATH
        for handler in root_logger.handlers
    )
    if not has_file:
        try:
            file_handler = RotatingFileHandler(
                LOG_FILE_PATH,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError as exc:
            root_logger.warning("无法打开日志文件 %s，仅输出到控制台: %s", LOG_FILE_PATH, exc)


_setup_logging()

sleep_time = 0.05
refresh_sleep_time = 0.5
retry_time = 30

window_title = "网上股票交易系统5.0"
# 主窗口过小时（如宿主机重启后客户端以很小的窗口启动），底部持仓/成交/委托区域不可见，
# F6/F7/F8 等快捷键切页失效，复制到的始终是持仓表。低于该尺寸时自动放大窗口。
MIN_MAIN_WINDOW_WIDTH = 640
MIN_MAIN_WINDOW_HEIGHT = 480
TARGET_MAIN_WINDOW_WIDTH = 1024
TARGET_MAIN_WINDOW_HEIGHT = 720
TASKBAR_RESERVED_HEIGHT = 40

RIGHT_PANEL_CONTROL_ID = 0xE901
COMMON_GRID_CONTROL_ID = 0x417
TREE_CONTROL_ID = 129
LEFT_BOTTOM_TAB_CONTROL_ID = 0x98A
CANCEL_NO_CONFIRM_CHECKBOX_ID = 0x96B
CAPTCHA_EDITOR_CONTROL_ID = 0x964
CAPTCHA_IMAGE_CONTROL_ID = 0x965
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
WM_PASTE = 0x0302
BM_GETCHECK = 0x00F0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SPI_GETWORKAREA = 0x0030
ES_PASSWORD = 0x0020
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2

_TABLE_IDENTITY_COLUMNS = (
    "证券代码",
    "合同编号",
    "证券名称",
    "委托编号",
    "成交编号",
    "流水号",
    "申购代码",
    "基金代码",
    "股东代码",
)

_SEND_KEYS_TOKEN_MAP = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "tab": "{TAB}",
    "space": "{SPACE}",
    "delete": "{DELETE}",
    "del": "{DELETE}",
    "backspace": "{BACKSPACE}",
    "down": "{DOWN}",
    "up": "{UP}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "down_arrow": "{DOWN}",
    "up_arrow": "{UP}",
    "left_arrow": "{LEFT}",
    "right_arrow": "{RIGHT}",
    "page_up": "{PGUP}",
    "page_down": "{PGDN}",
    "pgup": "{PGUP}",
    "pgdn": "{PGDN}",
    "home": "{HOME}",
    "end": "{END}",
}

AI_AGENT_SYSTEM_PROMPT = """你是同花顺Windows股票交易客户端（网上股票交易系统5.0）的GUI自动化智能体。
你的目标是完全按照用户的指令操作同花顺客户端，不受限制地执行任何功能（如查询各类报表、银证转账、国债逆回购、新股/可转债申购、条件单、融资融券、科创板/北交所交易、修改设置、处理弹窗等）并返回准确结果。

每一轮你将收到：
1. 当前同花顺主窗口截图（若存在弹窗也会体现在画面与状态文本中）。
2. 当前窗口的精确坐标与Win32控件树摘要（包括主窗口区域、弹窗列表、左侧菜单树SysTreeView32节点、可见按钮/输入框/标签/表格CVirtualGridCtrl及其handle、control_id、相对主窗口坐标rel_rect与中心点center）。
3. 已执行的步骤历史与上一步动作的返回数据（例如复制出的表格内容等）。

【坐标系说明】
- 默认所有点击坐标 (x, y) 均为相对主窗口左上角的像素坐标（即截图内的像素坐标，范围 0 <= x <= width, 0 <= y <= height）。
- 如需使用屏幕绝对坐标，可在动作参数中设置 "coord_type": "screen"。
- 如果目标控件在控件列表中有 handle，优先指定 handle 操作（如 click / input_text / copy_grid），比纯坐标更精准。

【可用动作（每次只能输出一个 JSON 对象）】
1. click: 单击
   {"thought": "...", "action": "click", "params": {"x": 120, "y": 85, "handle": 可选整数句柄, "button": "left"}}
2. double_click: 双击（常用于左侧树节点展开/进入、表格行撤单或触发详情）
   {"thought": "...", "action": "double_click", "params": {"x": 120, "y": 85, "handle": 可选整数句柄}}
3. right_click: 右键单击
   {"thought": "...", "action": "right_click", "params": {"x": 120, "y": 85, "handle": 可选整数句柄}}
4. select_tree_item: 直接选择或点击左侧功能菜单树（SysTreeView32）中的节点
   {"thought": "...", "action": "select_tree_item", "params": {"path": "新股申购\\新股申购"}} 或 {"text": "国债逆回购"}
5. input_text: 在输入框中填写文本（支持通过 handle、control_id 或 x,y 定位输入框）
   {"thought": "...", "action": "input_text", "params": {"handle": 12345, "control_id": 可选, "x": 可选, "y": 可选, "text": "600000", "clear": true, "press_enter": false}}
6. hot_key: 发送键盘按键或组合键（例如 ["F1"]、["F5"]、["enter"]、["esc"]、["ctrl", "c"]、["y"]）
   {"thought": "...", "action": "hot_key", "params": {"keys": ["F5"]}}
7. copy_grid: 复制当前页面表格（CVirtualGridCtrl）的全部内容并返回结构化JSON（自动处理同花顺复制验证码）
   {"thought": "...", "action": "copy_grid", "params": {"handle": 可选表格句柄, "control_id": 可选默认1047}}
8. get_control_text: 读取指定控件或弹窗的文本
   {"thought": "...", "action": "get_control_text", "params": {"handle": 可选, "control_id": 可选}}
9. scroll: 鼠标滚轮滚动
   {"thought": "...", "action": "scroll", "params": {"x": 300, "y": 300, "wheel_dist": -3}}
10. wait: 等待界面加载
    {"thought": "...", "action": "wait", "params": {"seconds": 1.0}}
11. switch_tab: 切换左下角大类标签（"normal" 普通交易 或 "kechuang" 科创板）
    {"thought": "...", "action": "switch_tab", "params": {"tab": "normal"}}
12. 内置快捷动作（当用户需求恰好包含这些基础操作时可直接调用）:
    - {"thought": "...", "action": "get_balance", "params": {}}
    - {"thought": "...", "action": "get_position", "params": {}}
    - {"thought": "...", "action": "get_active_orders", "params": {}}
    - {"thought": "...", "action": "get_filled_orders", "params": {}}
    - {"thought": "...", "action": "buy", "params": {"stock_no": "600000", "amount": 100, "price": 10.5}}
    - {"thought": "...", "action": "sell", "params": {"stock_no": "600000", "amount": 100, "price": 10.5}}
    - {"thought": "...", "action": "cancel", "params": {"entrust_no": "..."}}
13. finish: 任务已完成，返回最终结果
    {"thought": "...", "action": "finish", "result": <任意结构化数据或字典/列表/字符串>, "message": "简明总结"}
14. fail: 任务无法完成（例如客户端报错弹窗提示余额不足、休市、找不到对应菜单等）
    {"thought": "...", "action": "fail", "message": "失败原因说明", "result": <可选附加信息>}

【输出格式要求】
必须且只能输出一个合法的 JSON 对象，不要包含任何其他解释文字。"""

_SEND_KEYS_MODIFIER_MAP = {
    "ctrl": "^",
    "alt": "%",
    "shift": "+",
}


def _resolve_wrapper(control):
    if control is None:
        return None
    return control.wrapper_object() if hasattr(control, "wrapper_object") else control


def _to_send_keys_token(key):
    key = str(key)
    lower_key = key.lower()
    if lower_key in _SEND_KEYS_MODIFIER_MAP:
        return _SEND_KEYS_MODIFIER_MAP[lower_key]
    if lower_key in _SEND_KEYS_TOKEN_MAP:
        return _SEND_KEYS_TOKEN_MAP[lower_key]
    if key.upper().startswith("F") and key[1:].isdigit():
        return "{%s}" % key.upper()
    if len(key) == 1 and key in "^%+~(){}[]":
        return "{%s}" % key
    return key


def clear_clipboard():
    """使用 Win32 API 真正清空剪贴板，避免外部 cmd/clip 进程开销或残留文本。"""
    if hasattr(ctypes, "windll"):
        try:
            user32 = ctypes.windll.user32
            for _ in range(5):
                if user32.OpenClipboard(None):
                    try:
                        user32.EmptyClipboard()
                        return True
                    finally:
                        user32.CloseClipboard()
                time.sleep(sleep_time)
        except Exception:
            logging.debug("Win32 清空剪贴板失败，回退 cmd clip", exc_info=True)
    subprocess.run(["cmd", "/c", "clip"], input="", text=True, check=False)
    return False


def set_clipboard_unicode_text(value):
    """使用 Win32 CF_UNICODETEXT 写入剪贴板（兼容 32/64 位 Python，无编码与换行污染）。"""
    text = "" if value is None else str(value)
    if hasattr(ctypes, "windll"):
        try:
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32

            global_alloc = kernel32.GlobalAlloc
            global_alloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            global_alloc.restype = ctypes.c_void_p

            global_lock = kernel32.GlobalLock
            global_lock.argtypes = [ctypes.c_void_p]
            global_lock.restype = ctypes.c_void_p

            global_unlock = kernel32.GlobalUnlock
            global_unlock.argtypes = [ctypes.c_void_p]
            global_unlock.restype = ctypes.c_int

            global_free = kernel32.GlobalFree
            global_free.argtypes = [ctypes.c_void_p]
            global_free.restype = ctypes.c_void_p

            open_clipboard = user32.OpenClipboard
            open_clipboard.argtypes = [ctypes.c_void_p]
            open_clipboard.restype = ctypes.c_int

            empty_clipboard = user32.EmptyClipboard
            empty_clipboard.argtypes = []
            empty_clipboard.restype = ctypes.c_int

            set_clipboard_data = user32.SetClipboardData
            set_clipboard_data.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            set_clipboard_data.restype = ctypes.c_void_p

            close_clipboard = user32.CloseClipboard
            close_clipboard.argtypes = []
            close_clipboard.restype = ctypes.c_int

            encoded = text.encode("utf-16-le") + b"\x00\x00"
            for _ in range(5):
                if open_clipboard(None):
                    hmem = None
                    try:
                        empty_clipboard()
                        hmem = global_alloc(GMEM_MOVEABLE, len(encoded))
                        if not hmem:
                            raise MemoryError("GlobalAlloc failed")
                        ptr = global_lock(hmem)
                        if not ptr:
                            raise MemoryError("GlobalLock failed")
                        try:
                            ctypes.memmove(ptr, encoded, len(encoded))
                        finally:
                            global_unlock(hmem)
                        if not set_clipboard_data(CF_UNICODETEXT, hmem):
                            raise RuntimeError("SetClipboardData failed")
                        hmem = None
                        return
                    finally:
                        if hmem:
                            global_free(hmem)
                        close_clipboard()
                time.sleep(sleep_time)
        except Exception:
            logging.debug("Win32 写入剪贴板失败，回退 cmd clip", exc_info=True)
    subprocess.run(["cmd", "/c", "clip"], input=text, text=True, check=True)


def get_clipboard_data(max_attempts=5):
    for index in range(max_attempts):
        if hasattr(ctypes, "windll"):
            try:
                if not ctypes.windll.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    time.sleep(sleep_time)
                    continue
            except Exception:
                pass
        try:
            data = clipboard.GetData()
            logging.info(
                "剪贴板读取成功: attempt=%d size=%d",
                index + 1,
                len(data) if data else 0,
            )
            return data
        except Exception:
            logging.debug("剪贴板暂不可读: attempt=%d", index + 1, exc_info=True)
            time.sleep(sleep_time)
    logging.info("剪贴板读取结束: 未获取到数据")
    return None


def hot_key(keys: Iterable[str]):
    keys = list(keys)
    sequence = "".join(_to_send_keys_token(key) for key in keys)
    logging.info("发送按键: keys=%s sequence=%s", keys, sequence)
    time.sleep(sleep_time)
    keyboard.send_keys(
        sequence,
        pause=sleep_time,
        with_spaces=True,
        with_tabs=True,
        with_newlines=True,
        vk_packet=False,
    )


def set_text(control, string):
    editor = _resolve_wrapper(control)
    value = "" if string is None else str(string)
    logging.info(
        "设置文本: handle=%s class=%s value=%r",
        getattr(editor, "handle", "unknown"),
        editor.friendly_class_name() if hasattr(editor, "friendly_class_name") else type(editor).__name__,
        value,
    )
    editor.set_focus()
    try:
        editor.set_edit_text(value)
    except Exception:
        logging.exception("set_edit_text失败，回退到type_keys: handle=%s", getattr(editor, "handle", "unknown"))
        editor.click_input()
        keyboard.send_keys("{END}", pause=sleep_time, vk_packet=False)
        for _ in range(8):
            keyboard.send_keys("{BACKSPACE}", pause=sleep_time, vk_packet=False)
        if value:
            editor.type_keys(
                value,
                set_foreground=False,
                pause=sleep_time,
                with_spaces=True,
                with_tabs=True,
                with_newlines=True,
                vk_packet=False,
            )


def get_text(control):
    wrapper = _resolve_wrapper(control)
    if wrapper is None:
        return ""
    try:
        return wrapper.window_text()
    except Exception:
        return ""


def _class_name(control):
    try:
        return control.class_name()
    except Exception:
        return type(control).__name__


def _control_id(control):
    try:
        return control.control_id()
    except Exception:
        return None


def _rect_tuple(control):
    rect = control.rectangle()
    return rect.left, rect.top, rect.right, rect.bottom


def _parse_decimal_safe(value):
    if value is None:
        return None
    cleaned = str(value).strip().replace(",", "")
    if not cleaned or cleaned in ("-", "--", "—", "N/A", "nan", "None"):
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_trade_price(price) -> str:
    """规范化委托价格：消除二进制浮点误差，兼容 A 股 2 位小数与 ETF/可转债/逆回购 3 位小数。"""
    dec = _parse_decimal_safe(price)
    if dec is None:
        raise ValueError(f"无效的委托价格: {price!r}")
    if dec <= 0:
        raise ValueError(f"委托价格必须大于 0: {price!r}")
    quantized = dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if text.endswith("0"):
        text = text[:-1]
    return text


def _is_placeholder_or_summary_row(row: dict, keys: list) -> bool:
    if not row:
        return True
    stripped_values = [str(v).strip() for v in row.values()]
    if not any(stripped_values):
        return True
    present_id_cols = [col for col in _TABLE_IDENTITY_COLUMNS if col in row]
    if present_id_cols:
        if all(not str(row.get(col, "")).strip() for col in present_id_cols):
            return True
    return False


def parse_table(text):
    if not text:
        return []
    lines = text.split("\t\r\n")
    keys = [key for key in lines[0].split("\t") if key != ""]
    if not keys:
        return []
    result = []
    for i in range(1, len(lines)):
        if not lines[i]:
            continue
        info = {}
        items = lines[i].split("\t")
        for j in range(min(len(keys), len(items))):
            info[keys[j]] = items[j]
        if info and not _is_placeholder_or_summary_row(info, keys):
            result.append(info)
    return result


class ThsAuto:

    def __init__(self):
        self.app = None
        self.main_window = None
        self.hwnd_main = None

    def _ensure_bound(self):
        if self.main_window is None or self.hwnd_main is None or self.app is None:
            self.bind_client()
        if self.main_window is None or self.hwnd_main is None or self.app is None:
            raise RuntimeError("未找到同花顺客户端窗口，请确认客户端已启动且主窗口可见")

    def _main_spec(self):
        self._ensure_bound()
        return self.app.window(handle=self.hwnd_main)

    def _child_window(self, parent, timeout=3, **kwargs):
        self._ensure_bound()
        parent = _resolve_wrapper(parent)
        spec = self.app.window(handle=parent.handle).child_window(**kwargs)
        spec.wait("exists ready", timeout=timeout)
        wrapper = spec.wrapper_object()
        logging.info(
            "定位子控件成功: parent=%s criteria=%s handle=%s class=%s",
            getattr(parent, "handle", "unknown"),
            kwargs,
            getattr(wrapper, "handle", "unknown"),
            wrapper.class_name() if hasattr(wrapper, "class_name") else type(wrapper).__name__,
        )
        return wrapper

    def _descendant_windows(self, parent, visible_only=True, enabled_only=False, **kwargs):
        parent = _resolve_wrapper(parent)
        candidates = []
        for ctrl in parent.descendants(**kwargs):
            try:
                if visible_only and not ctrl.is_visible():
                    continue
                if enabled_only and not ctrl.is_enabled():
                    continue
            except Exception:
                pass
            candidates.append(ctrl)
        return candidates

    @staticmethod
    def _control_area(control):
        rect = control.rectangle()
        return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)

    def _pick_descendant(self, parent, chooser, description, **kwargs):
        candidates = self._descendant_windows(parent, **kwargs)
        if not candidates:
            raise RuntimeError("未找到控件: %s" % description)
        if len(candidates) == 1:
            return candidates[0]
        selected = chooser(candidates)
        logging.info(
            "发现%d个%s候选控件，选择 handle=%s",
            len(candidates),
            description,
            getattr(selected, "handle", "unknown"),
        )
        return selected

    def _find_from_candidates(self, parents, timeout=3, **kwargs):
        last_error = None
        for parent in parents:
            if parent is None:
                continue
            try:
                return self._child_window(parent, timeout=timeout, **kwargs)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("未找到控件: %s" % kwargs)

    def _get_right_control(self, control_id, class_name=None, timeout=3):
        self._ensure_bound()
        kwargs = {"control_id": control_id}
        if class_name is not None:
            kwargs["class_name"] = class_name
        right_panel = self.get_right_hwnd()
        return self._find_from_candidates(
            [right_panel, self.main_window], timeout=timeout, **kwargs
        )

    def _popup_windows(self):
        self._ensure_bound()
        windows = []
        handles = set()
        try:
            top_window = self.app.top_window().wrapper_object()
            if top_window.handle != self.hwnd_main:
                windows.append(top_window)
                handles.add(top_window.handle)
        except Exception:
            pass
        try:
            for window in self.app.windows(visible_only=True):
                wrapper = _resolve_wrapper(window)
                handle = getattr(wrapper, "handle", None)
                if handle and handle != self.hwnd_main and handle not in handles:
                    windows.append(wrapper)
                    handles.add(handle)
        except Exception:
            pass
        logging.info("检测到弹窗数量: %d", len(windows))
        return windows

    @staticmethod
    def _is_persistent_tool_window(window) -> bool:
        try:
            return bool(window.exstyle() & WS_EX_TOOLWINDOW and not (window.style() & WS_CAPTION))
        except Exception:
            return False

    def _dismiss_stubborn_popup(self, popup):
        """关闭不响应 Esc 的模态弹窗（如仅含 是/否 按钮的对话框），严禁点击‘是’。"""
        try:
            descendants = popup.descendants(class_name="Button")
            if isinstance(descendants, Iterable):
                for btn in descendants:
                    try:
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                        cid = _control_id(btn)
                        text = get_text(btn).strip()
                        if cid == 7 or any(token in text for token in ("否(&N)", "否(N)", "取消")) or text == "否":
                            logging.info("点击取消/否按钮关闭残留弹窗: handle=%s text=%r", getattr(popup, "handle", None), text)
                            btn.click_input()
                            return
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            logging.info("发送 WM_CLOSE 关闭不响应 Esc 的残留弹窗: handle=%s", getattr(popup, "handle", None))
            popup.close()
        except Exception:
            logging.debug("发送 WM_CLOSE 关闭残留弹窗失败", exc_info=True)

    def dismiss_popups(self, max_attempts=20):
        """执行命令前逐个关闭本客户端的残留提示窗口。"""
        if self.app is None or self.main_window is None:
            # 客户端未运行时无需清理，后续命令仍按各自流程处理。
            if not self.bind_client(timeout=2, log_failure=False):
                return 0

        escape_count = 0
        last_escaped_handle = None
        for attempt in range(max_attempts + 1):
            popup = None
            for window in self._popup_windows():
                try:
                    # 多层模态窗口中只有最上层可操作，每次按键后重新枚举。
                    if window.is_visible() and window.is_enabled():
                        # 同花顺有常驻的无标题栏工具窗口（也使用 #32770 类），
                        # 不是提示框，不会响应 Esc，不能仅凭可见性判断。
                        if self._is_persistent_tool_window(window):
                            continue
                        popup = window
                        break
                except Exception:
                    # 窗口可能在枚举后自行消失。
                    continue

            if popup is None and self.main_window.is_enabled():
                if escape_count:
                    logging.info("残留提示窗口清理完成，共发送 %s 次关闭指令", escape_count)
                return escape_count

            if attempt == max_attempts:
                break

            if popup is not None:
                current_handle = getattr(popup, "handle", None)
                try:
                    popup.set_focus()
                    hot_key(["esc"])
                    escape_count += 1
                    logging.info("向残留提示窗口发送 Esc: handle=%s", current_handle)
                    if current_handle is not None and current_handle == last_escaped_handle:
                        self._dismiss_stubborn_popup(popup)
                    last_escaped_handle = current_handle
                except Exception:
                    logging.debug("关闭残留提示窗口失败，将重新检查", exc_info=True)
            time.sleep(0.2)

        raise RuntimeError(
            f"尝试 {max_attempts} 次后仍有残留提示窗口或主窗口不可操作，已停止执行命令"
        )

    def _window_static_texts(self, window):
        texts = []
        try:
            for ctrl in window.descendants(class_name="Static"):
                text = get_text(ctrl).strip()
                if text:
                    texts.append(text)
        except Exception:
            logging.exception("读取弹窗静态文本失败: handle=%s", getattr(window, "handle", "unknown"))
        return texts

    def _window_button_texts(self, window):
        texts = []
        try:
            for ctrl in window.descendants(class_name="Button"):
                text = get_text(ctrl).strip()
                if text:
                    texts.append(text)
        except Exception:
            logging.exception("读取弹窗按钮文本失败: handle=%s", getattr(window, "handle", "unknown"))
        return texts

    def _find_yes_button(self, popup):
        try:
            for btn in popup.descendants(class_name="Button"):
                try:
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                except Exception:
                    continue
                cid = _control_id(btn)
                text = get_text(btn).strip()
                if cid == 6 or "是(&Y)" in text or "是(Y)" in text or text == "是":
                    return btn
        except Exception:
            pass
        return None

    def _find_trade_confirm_popup(self):
        for popup in self._popup_windows():
            if self._is_persistent_tool_window(popup):
                continue
            try:
                if not popup.is_visible() or not popup.is_enabled():
                    continue
            except Exception:
                continue
            yes_btn = self._find_yes_button(popup)
            if yes_btn is not None:
                return popup, yes_btn
        return None, None

    def _find_cancel_confirm_popup(self):
        for popup in self._popup_windows():
            if self._is_persistent_tool_window(popup):
                continue
            static_texts = self._window_static_texts(popup)
            button_texts = self._window_button_texts(popup)
            joined_text = " ".join(static_texts + button_texts)
            if "撤单确认" in joined_text or "是(Y)" in joined_text or "是(&Y)" in joined_text:
                logging.info(
                    "检测到撤单确认弹窗: handle=%s static_texts=%s button_texts=%s",
                    getattr(popup, "handle", "unknown"),
                    static_texts,
                    button_texts,
                )
                return popup
        logging.info("未检测到撤单确认弹窗")
        return None

    def _dialog_children(self, dialog):
        children = []
        try:
            for child in dialog.children():
                try:
                    if not child.is_visible():
                        continue
                except Exception:
                    pass
                children.append(child)
        except Exception:
            logging.exception("读取弹窗子控件失败: dialog=%s", getattr(dialog, "handle", "unknown"))
        return children

    def _log_dialog_children(self, dialog):
        children = self._dialog_children(dialog)
        for index, child in enumerate(children):
            rect = child.rectangle()
            logging.info(
                "弹窗子控件: index=%d handle=%s class=%s control_id=%s text=%r rect=(%d,%d,%d,%d)",
                index,
                getattr(child, "handle", "unknown"),
                _class_name(child),
                _control_id(child),
                get_text(child),
                rect.left,
                rect.top,
                rect.right,
                rect.bottom,
            )
        return children

    def _get_ocr_controls(self, dialog):
        children = self._log_dialog_children(dialog)
        anchor = None
        for child in children:
            text = get_text(child).strip()
            if "检测到您正在拷贝数据" in text:
                anchor = child
                break

        sibling_image = None
        sibling_editor = None
        sibling_error = None
        if anchor is not None:
            anchor_index = children.index(anchor)
            logging.info(
                "验证码提示锚点定位成功: handle=%s index=%d",
                getattr(anchor, "handle", "unknown"),
                anchor_index,
            )
            for child in children[anchor_index + 1 :]:
                if sibling_image is None and _class_name(child) == "Static":
                    sibling_image = child
                    continue
                if sibling_editor is None and _class_name(child) == "Edit":
                    sibling_editor = child
                    continue
                if sibling_image is not None and sibling_error is None and _class_name(child) == "Static":
                    sibling_error = child

        fixed_image = None
        fixed_editor = None
        ok_button = None
        error_label = None
        try:
            fixed_image = self._child_window(
                dialog,
                control_id=CAPTCHA_IMAGE_CONTROL_ID,
                class_name="Static",
                timeout=0.5,
            )
        except Exception:
            logging.info("未通过固定control_id定位到验证码图片控件")
        try:
            fixed_editor = self._child_window(
                dialog,
                control_id=CAPTCHA_EDITOR_CONTROL_ID,
                class_name="Edit",
                timeout=0.5,
            )
        except Exception:
            logging.info("未通过固定control_id定位到验证码输入框")

        image = sibling_image or fixed_image
        editor = fixed_editor or sibling_editor
        error_candidates = [
            child
            for child in children
            if _class_name(child) == "Static" and "验证码错误" in get_text(child)
        ]
        if error_candidates:
            error_label = error_candidates[0]
        elif sibling_error is not None and "验证码错误" in get_text(sibling_error):
            error_label = sibling_error

        button_candidates = [
            child
            for child in children
            if _class_name(child) == "Button"
        ]
        for child in button_candidates:
            if _control_id(child) == 1 or "确定" in get_text(child):
                ok_button = child
                break

        if image is None:
            static_candidates = [
                child
                for child in children
                if _class_name(child) == "Static" and not get_text(child).strip()
            ]
            if static_candidates:
                image = max(static_candidates, key=self._control_area)
                logging.info(
                    "使用空白Static候选作为验证码图片控件: handle=%s",
                    getattr(image, "handle", "unknown"),
                )

        if editor is None:
            edit_candidates = [child for child in children if _class_name(child) == "Edit"]
            if edit_candidates:
                if image is not None:
                    image_left, image_top, image_right, image_bottom = _rect_tuple(image)
                    editor = min(
                        edit_candidates,
                        key=lambda ctrl: (
                            abs(ctrl.rectangle().top - image_top),
                            abs(ctrl.rectangle().left - image_left),
                            self._control_area(ctrl),
                        ),
                    )
                else:
                    editor = edit_candidates[0]
                logging.info(
                    "使用Edit候选作为验证码输入框: handle=%s",
                    getattr(editor, "handle", "unknown"),
                )

        logging.info(
            "验证码控件选择结果: image=%s editor=%s error_label=%s ok_button=%s fixed_image=%s fixed_editor=%s",
            getattr(image, "handle", None),
            getattr(editor, "handle", None),
            getattr(error_label, "handle", None),
            getattr(ok_button, "handle", None),
            getattr(fixed_image, "handle", None),
            getattr(fixed_editor, "handle", None),
        )
        return image, editor, error_label, ok_button

    def _ocr_editor_value(self, editor):
        handle = getattr(editor, "handle", None)
        value = ""
        if handle:
            try:
                length = ctypes.windll.user32.SendMessageW(handle, WM_GETTEXTLENGTH, 0, 0)
                buffer = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.SendMessageW(
                    handle,
                    WM_GETTEXT,
                    length + 1,
                    ctypes.cast(buffer, ctypes.c_void_p).value,
                )
                value = buffer.value.strip()
            except Exception:
                logging.exception("验证码输入框消息回读失败: handle=%s", handle)
        if not value:
            value = get_text(editor).strip()
        logging.info(
            "验证码输入框当前文本: handle=%s value=%r",
            getattr(editor, "handle", "unknown"),
            value,
        )
        return value

    def _send_text_message(self, editor, message, value, label):
        handle = getattr(editor, "handle", None)
        if not handle:
            raise RuntimeError("验证码输入框没有有效句柄")
        text_buffer = ctypes.create_unicode_buffer(str(value))
        result = ctypes.windll.user32.SendMessageW(
            handle,
            message,
            0,
            ctypes.cast(text_buffer, ctypes.c_void_p).value,
        )
        logging.info(
            "%s完成: handle=%s value=%r result=%s",
            label,
            handle,
            value,
            result,
        )
        return result

    def _send_wm_settext(self, editor, value):
        return self._send_text_message(editor, WM_SETTEXT, value, "SendMessageW(WM_SETTEXT)")

    def _send_em_replacesel(self, editor, value):
        handle = getattr(editor, "handle", None)
        if not handle:
            raise RuntimeError("验证码输入框没有有效句柄")
        ctypes.windll.user32.SendMessageW(handle, EM_SETSEL, 0, -1)
        text_buffer = ctypes.create_unicode_buffer(str(value))
        result = ctypes.windll.user32.SendMessageW(
            handle,
            EM_REPLACESEL,
            1,
            ctypes.cast(text_buffer, ctypes.c_void_p).value,
        )
        logging.info(
            "SendMessageW(EM_REPLACESEL)完成: handle=%s value=%r result=%s",
            handle,
            value,
            result,
        )
        return result

    def _clear_ocr_editor(self, editor):
        self._clear_editor_by_backspace(editor, context="验证码输入框")

    def _set_clipboard_text(self, value):
        logging.info("准备写入剪贴板: value=%r", value)
        set_clipboard_unicode_text(value)
        logging.info("文本已写入剪贴板")

    def _editor_value(self, editor):
        return self._ocr_editor_value(editor)

    def _clear_editor_by_backspace(self, editor, context="编辑框", steps=8):
        logging.info("%s 开始清空: 使用End+%d次Backspace", context, steps)
        try:
            editor.click_input()
            time.sleep(sleep_time)
            keyboard.send_keys("{END}", pause=sleep_time, vk_packet=False)
            for index in range(steps):
                keyboard.send_keys("{BACKSPACE}", pause=sleep_time, vk_packet=False)
                logging.info(
                    "%s Backspace清空进度: handle=%s step=%d/%d",
                    context,
                    getattr(editor, "handle", "unknown"),
                    index + 1,
                    steps,
                )
        except Exception:
            logging.exception("%s End+Backspace清空失败: handle=%s", context, getattr(editor, "handle", "unknown"))
        time.sleep(sleep_time)

    def _clear_editor(self, editor, context="编辑框"):
        self._clear_editor_by_backspace(editor, context=context, steps=8)

    def _paste_editor_text(self, editor, value, context="编辑框"):
        editor.click_input()
        time.sleep(sleep_time)
        self._set_clipboard_text(value)
        try:
            handle = getattr(editor, "handle", None)
            if handle:
                result = ctypes.windll.user32.SendMessageW(handle, WM_PASTE, 0, 0)
                logging.info(
                    "%s SendMessageW(WM_PASTE)完成: handle=%s value=%r result=%s",
                    context,
                    handle,
                    value,
                    result,
                )
            else:
                raise RuntimeError(f"{context}没有有效句柄")
        except Exception:
            logging.exception("%s 消息粘贴失败，回退控件粘贴: handle=%s", context, getattr(editor, "handle", "unknown"))
            try:
                editor.type_keys("^v", set_foreground=False, pause=sleep_time, vk_packet=False)
            except Exception:
                logging.exception("%s 控件粘贴失败，回退全局粘贴: handle=%s", context, getattr(editor, "handle", "unknown"))
                keyboard.send_keys("^v", pause=sleep_time, vk_packet=False)
        time.sleep(sleep_time)

    def _prepare_price_field(self, editor):
        logging.info("开始准备价格字段，等待证券代码触发自动价格回填")
        time.sleep(0.8)
        observed = self._editor_value(editor)
        logging.info("价格字段自动回填观测值: %r", observed)
        time.sleep(sleep_time)

    def _clear_price_editor(self, editor):
        self._clear_editor_by_backspace(editor, context="价格字段", steps=8)

    @staticmethod
    def _trade_field_matches(expected: str, actual: str, field_name: str) -> bool:
        if actual == expected:
            return True
        if field_name in ("买卖价格", "买卖数量"):
            exp_dec = _parse_decimal_safe(expected)
            act_dec = _parse_decimal_safe(actual)
            if exp_dec is not None and act_dec is not None and exp_dec == act_dec:
                return True
        return False

    def _set_trade_field(self, control_id, value, field_name):
        editor = self._get_right_control(control_id, class_name="Edit")
        expected = "" if value is None else str(value)
        logging.info(
            "开始填写交易字段: field=%s control_id=%s handle=%s expected=%r",
            field_name,
            hex(control_id),
            getattr(editor, "handle", "unknown"),
            expected,
        )
        actual = ""
        for attempt in range(2):
            logging.info(
                "交易字段填写尝试: field=%s attempt=%d/2",
                field_name,
                attempt + 1,
            )
            if field_name == "买卖价格":
                self._prepare_price_field(editor)
                self._clear_price_editor(editor)
            else:
                self._clear_editor(editor, context=field_name)
            self._paste_editor_text(editor, expected, context=field_name)
            actual = self._editor_value(editor)
            logging.info(
                "交易字段填写结果: field=%s expected=%r actual=%r",
                field_name,
                expected,
                actual,
            )
            if self._trade_field_matches(expected, actual, field_name):
                return editor
            if actual:
                logging.info(
                    "交易字段回读不匹配，准备重试: field=%s expected=%r actual=%r",
                    field_name,
                    expected,
                    actual,
                )
            else:
                logging.info(
                    "交易字段回读为空，按不可回读字段继续: field=%s expected=%r",
                    field_name,
                    expected,
                )
                return editor
        raise RuntimeError(f"{field_name}填写失败，期望值={expected!r}，实际值={actual!r}")

    def _paste_ocr_code(self, editor, value):
        editor.click_input()
        time.sleep(sleep_time)
        self._set_clipboard_text(value)
        try:
            handle = getattr(editor, "handle", None)
            if handle:
                result = ctypes.windll.user32.SendMessageW(handle, WM_PASTE, 0, 0)
                logging.info(
                    "SendMessageW(WM_PASTE)完成: handle=%s value=%r result=%s",
                    handle,
                    value,
                    result,
                )
            else:
                raise RuntimeError("验证码输入框没有有效句柄")
        except Exception:
            logging.exception("验证码输入框消息粘贴失败，回退按键粘贴: handle=%s", getattr(editor, "handle", "unknown"))
            keyboard.send_keys("^v", pause=sleep_time, vk_packet=False)

    def _ocr_input_strategies(self, editor, code):
        value = str(code).strip()
        return [
            ("clipboard_paste", lambda: self._paste_ocr_code(editor, value)),
        ]

    def _apply_ocr_input_strategy(self, editor, code, strategy_name, strategy):
        value = str(code).strip()
        logging.info(
            "尝试验证码输入策略: strategy=%s handle=%s value=%r",
            strategy_name,
            getattr(editor, "handle", "unknown"),
            value,
        )
        self._clear_ocr_editor(editor)
        try:
            editor.set_focus()
        except Exception:
            logging.exception("验证码输入框聚焦失败: handle=%s", getattr(editor, "handle", "unknown"))
        strategy()
        time.sleep(sleep_time)
        current_value = self._ocr_editor_value(editor)
        if current_value == value:
            logging.info("验证码输入校验成功: strategy=%s", strategy_name)
        else:
            logging.info(
                "验证码输入校验未确认: strategy=%s expected=%r actual=%r",
                strategy_name,
                value,
                current_value,
            )

    def _get_ocr_error_text(self, dialog, error_label=None):
        if error_label is not None:
            text = get_text(error_label).strip()
            if "验证码错误" in text:
                return text
        for ctrl in dialog.descendants(class_name="Static"):
            text = get_text(ctrl).strip()
            if "验证码错误" in text:
                return text
        return ""

    def _submit_ocr_dialog(self, dialog, ok_button=None):
        if ok_button is not None:
            logging.info("点击验证码确定按钮提交: handle=%s", getattr(ok_button, "handle", "unknown"))
            ok_button.click_input()
        else:
            logging.info("未定位到验证码确定按钮，回退回车提交")
            dialog.set_focus()
            hot_key(["enter"])

    def _wait_ocr_submit_result(self, dialog, error_label=None, timeout=1.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_dialog = self.get_ocr_hwnd()
            if current_dialog is None:
                logging.info("验证码提交后弹窗已关闭")
                return "closed", ""
            error_text = self._get_ocr_error_text(current_dialog, error_label)
            if error_text:
                logging.info("验证码提交后检测到错误提示: %r", error_text)
                return "error", error_text
            time.sleep(0.1)
        logging.info("验证码提交后等待超时，未检测到关闭或错误提示")
        return "pending", ""

    @staticmethod
    def _image_has_content(image):
        grayscale = image.convert("L")
        low, high = grayscale.getextrema()
        return (high - low) > 3

    def _window_has_static_text(self, window, text):
        if text in get_text(window):
            return True
        try:
            for ctrl in window.descendants(class_name="Static"):
                if text in get_text(ctrl):
                    return True
        except Exception:
            return False
        return False

    def _wait_clipboard_table(self, control, copy_each_retry=False, ocr_kwargs=None):
        data = None
        max_attempts = 3 if copy_each_retry else 2
        for attempt in range(max_attempts):
            logging.info(
                "等待表格数据: attempt=%d/%d copy_each_retry=%s handle=%s",
                attempt + 1,
                max_attempts,
                copy_each_retry,
                getattr(control, "handle", "unknown"),
            )
            self.copy_table(control, ocr_kwargs=ocr_kwargs)
            time.sleep(sleep_time)
            data = get_clipboard_data(max_attempts=3)
            if data:
                logging.info("表格数据获取成功: rows_hint=%d", max(0, data.count("\t\r\n") - 1))
                return data
        logging.info("表格数据获取失败: 已达到最大重试次数(%d)", max_attempts)
        return data

    def _fill_trade_form(self, stock_no, amount, price):
        stock_str = str(stock_no or "").strip()
        if not stock_str:
            raise ValueError("证券代码不能为空")
        amount_int = int(amount)
        if amount_int <= 0:
            raise ValueError(f"委托数量必须为正整数: {amount!r}")
        price_str = format_trade_price(price) if price is not None else None

        self._set_trade_field(0x408, stock_str, "证券代码")
        time.sleep(refresh_sleep_time)
        if price_str is not None:
            self._set_trade_field(0x409, price_str, "买卖价格")
            time.sleep(sleep_time)
        self._set_trade_field(0x40A, str(amount_int), "买卖数量")
        time.sleep(sleep_time)

    def _close_result_popup(self, popup):
        if popup is None:
            return
        try:
            for btn in popup.descendants(class_name="Button"):
                try:
                    if not btn.is_visible() or not btn.is_enabled():
                        continue
                except Exception:
                    continue
                cid = _control_id(btn)
                text = get_text(btn).strip()
                if cid in (1, 2) or text == "确定" or "确定(" in text:
                    btn.click_input()
                    return
        except Exception:
            pass
        try:
            popup.close()
        except Exception:
            hot_key(["enter"])

    def _submit_trade_result(self, success_log, failed_log):
        hot_key(["enter"])
        max_polls = max(retry_time, 50)
        last_confirmed_handle = None
        last_confirm_ts = 0.0
        for _ in range(max_polls):
            time.sleep(0.1)
            result = self.get_result(close_popup=True)
            if result:
                logging.info(success_log if result.get("code") == 0 else failed_log)
                return result
            confirm_popup, yes_btn = self._find_trade_confirm_popup()
            if confirm_popup is not None:
                handle = getattr(confirm_popup, "handle", None)
                now = time.time()
                if handle != last_confirmed_handle or (now - last_confirm_ts) >= 0.4:
                    logging.info("检测到委托确认弹窗，执行确认: handle=%s", handle)
                    try:
                        yes_btn.click_input()
                    except Exception:
                        confirm_popup.set_focus()
                        hot_key(["y"])
                    last_confirmed_handle = handle
                    last_confirm_ts = now
        logging.info(failed_log)
        return {
            "code": 2,
            "status": "unknown",
            "msg": "获取结果失败,请自行确认订单状态",
        }

    def _is_cancel_no_confirm_enabled(self) -> bool:
        if not hasattr(ctypes, "windll"):
            return False
        try:
            chk = self._get_right_control(CANCEL_NO_CONFIRM_CHECKBOX_ID, class_name="Button", timeout=0.3)
            handle = getattr(chk, "handle", None)
            if handle:
                return bool(ctypes.windll.user32.SendMessageW(handle, BM_GETCHECK, 0, 0) == 1)
        except Exception:
            pass
        return False

    def _submit_cancel_result(self, entrust_no=None):
        no_confirm_mode = self._is_cancel_no_confirm_enabled()
        hot_key(["enter"])
        seen_confirm = False
        last_confirm_ts = 0.0
        idle_rounds = 0
        max_polls = max(retry_time, 30)

        for index in range(max_polls):
            time.sleep(0.1)
            result_popup = self.get_result(close_popup=True)
            if result_popup is not None:
                if result_popup.get("code") == 0 or "撤单" in result_popup.get("msg", "") and "失败" not in result_popup.get("msg", ""):
                    return {"code": 0, "status": "succeed", "msg": result_popup.get("msg", "")}
                return result_popup

            confirm_popup = self._find_cancel_confirm_popup()
            logging.info(
                "撤单提交流程检查确认窗: attempt=%d/%d confirm_popup=%s no_confirm=%s",
                index + 1,
                max_polls,
                getattr(confirm_popup, "handle", None),
                no_confirm_mode,
            )
            if confirm_popup is not None:
                static_joined = " ".join(self._window_static_texts(confirm_popup))
                if entrust_no is not None and "合同编号" in static_joined and str(entrust_no) not in static_joined:
                    logging.error(
                        "撤单确认弹窗合同编号不匹配，中止撤单! expected=%s popup_text=%r",
                        entrust_no,
                        static_joined,
                    )
                    self._dismiss_stubborn_popup(confirm_popup)
                    return {"code": 1, "status": "failed", "msg": "撤单目标行校验不一致，已安全取消"}
                seen_confirm = True
                now = time.time()
                if now - last_confirm_ts >= 0.4:
                    yes_btn = self._find_yes_button(confirm_popup)
                    try:
                        if yes_btn is not None:
                            yes_btn.click_input()
                        else:
                            confirm_popup.set_focus()
                            hot_key(["y"])
                    except Exception:
                        hot_key(["y"])
                    last_confirm_ts = now
                continue

            if seen_confirm:
                logging.info("撤单确认弹窗已消失")
                return {"code": 0, "status": "succeed"}

            if no_confirm_mode and self.main_window.is_enabled():
                idle_rounds += 1
                if idle_rounds >= 4:
                    logging.info("客户端已开启‘撤单不需要确认’且无异常弹窗，撤单指令已提交")
                    return {"code": 0, "status": "succeed"}

        logging.info("撤单确认弹窗未在预期时间内完成")
        return {
            "code": 2,
            "status": "unknown",
            "msg": "撤单确认窗口未关闭,请自行确认订单状态",
        }

    def bind_client(self, timeout=5, log_failure=True):
        self.app = None
        self.main_window = None
        self.hwnd_main = None
        try:
            logging.info("开始绑定客户端窗口: title=%s timeout=%s", window_title, timeout)
            self.app = Application(backend="win32").connect(title=window_title, timeout=timeout)
            main_window = self.app.window(title=window_title)
            # 残留模态提示框可能禁用主窗口，先完成绑定以便清理提示框。
            main_window.wait("exists", timeout=timeout)
            self.main_window = main_window.wrapper_object()
            self.hwnd_main = self.main_window.handle
            logging.info("客户端绑定成功: hwnd=%s", self.hwnd_main)
            if self.main_window.is_enabled():
                self.active_main_window()
            return True
        except Exception:
            if log_failure:
                logging.exception("客户端绑定失败: title=%s", window_title)
            else:
                logging.info("客户端绑定未完成: title=%s timeout=%s", window_title, timeout)
            self.app = None
            self.main_window = None
            self.hwnd_main = None
            return False

    def kill_client(self):
        retry = 5
        while retry > 0:
            self.bind_client(timeout=2, log_failure=False)
            if self.hwnd_main is None:
                logging.info("客户端已关闭或当前未检测到主窗口")
                break
            try:
                self.active_main_window()
                hot_key(["alt", "F4"])
            except Exception:
                try:
                    self.main_window.close()
                except Exception:
                    pass
            time.sleep(1)
            retry -= 1
        self.app = None
        self.main_window = None
        self.hwnd_main = None

    def get_tree_hwnd(self):
        self._ensure_bound()
        return self._pick_descendant(
            self.main_window,
            lambda candidates: min(
                candidates,
                key=lambda ctrl: (
                    ctrl.rectangle().left,
                    ctrl.rectangle().top,
                    -self._control_area(ctrl),
                ),
            ),
            "SysTreeView32",
            control_id=TREE_CONTROL_ID,
            class_name="SysTreeView32",
        )

    def get_right_hwnd(self):
        self._ensure_bound()
        return self._child_window(self.main_window, control_id=RIGHT_PANEL_CONTROL_ID)

    def _find_left_bottom_tabs(self):
        """查找旧版客户端左下角的‘普通/科创板’切换栏；新版合并界面中该控件隐藏，返回 None。"""
        self._ensure_bound()
        try:
            right_left = self.get_right_hwnd().rectangle().left
        except Exception:
            right_left = None

        candidates = []
        for ctrl in self._descendant_windows(self.main_window, visible_only=True, class_name="CCustomTabCtrl"):
            try:
                if self._control_area(ctrl) <= 0:
                    continue
                cid = _control_id(ctrl)
                rect = ctrl.rectangle()
                if cid == LEFT_BOTTOM_TAB_CONTROL_ID or (right_left is not None and rect.left < right_left):
                    candidates.append(ctrl)
            except Exception:
                continue
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda ctrl: (
                ctrl.rectangle().top,
                -ctrl.rectangle().left,
                self._control_area(ctrl),
            ),
        )

    def get_left_bottom_tabs(self):
        tabs = self._find_left_bottom_tabs()
        if tabs is not None:
            return tabs
        raise RuntimeError("当前客户端未显示左下角独立分类标签栏")

    def get_ocr_hwnd(self):
        for popup in self._popup_windows():
            if self._window_has_static_text(popup, "检测到您正在拷贝数据") or self._window_has_static_text(
                popup, "验证码"
            ):
                logging.info(
                    "检测到OCR弹窗: handle=%s texts=%s",
                    getattr(popup, "handle", "unknown"),
                    self._window_static_texts(popup),
                )
                return popup
        logging.info("未检测到OCR弹窗")
        return None

    def get_balance(self):
        logging.info("开始获取账户余额信息...")
        self.switch_to_normal()
        hot_key(["F4"])
        self.refresh()
        data = {}
        for key, cid in BALANCE_CONTROL_ID_GROUP.items():
            try:
                ctrl = self._get_right_control(cid, class_name="Static", timeout=1.5)
            except Exception:
                continue
            value = get_text(ctrl).strip()
            if value:
                data[key] = value
        if (
            "可用金额" not in data
            and "总资产" in data
            and "股票市值" in data
            and "冻结金额" in data
        ):
            total_asset = _parse_decimal_safe(data["总资产"])
            market_val = _parse_decimal_safe(data["股票市值"])
            frozen_val = _parse_decimal_safe(data["冻结金额"])
            if total_asset is not None and market_val is not None and frozen_val is not None:
                data["可用金额"] = str(total_asset - market_val - frozen_val)
            else:
                logging.warning(
                    "部分资金字段无法解析为数值，跳过可用金额兜底计算: total=%r market=%r frozen=%r",
                    data.get("总资产"),
                    data.get("股票市值"),
                    data.get("冻结金额"),
                )
        logging.info("账户余额信息获取成功")
        return {
            "code": 0,
            "status": "succeed",
            "data": data,
        }

    def get_position(self):
        logging.info("开始获取持仓信息...")
        self.switch_to_normal()
        hot_key(["F1"])
        hot_key(["F6"])
        self.refresh()
        ctrl = self._get_right_control(COMMON_GRID_CONTROL_ID, class_name="CVirtualGridCtrl")
        logging.info("持仓表格控件定位成功: handle=%s", getattr(ctrl, "handle", "unknown"))

        data = self._wait_clipboard_table(ctrl, copy_each_retry=True)
        if data:
            logging.info("持仓信息获取成功")
            return {
                "code": 0,
                "status": "succeed",
                "data": parse_table(data),
            }
        logging.info("持仓信息获取失败")
        return {"code": 1, "status": "failed", "msg": "获取持仓表格数据失败"}

    def get_active_orders(self):
        logging.info("开始获取当前委托信息...")
        self.switch_to_normal()
        hot_key(["F1"])
        hot_key(["F8"])
        self.refresh()
        ctrl = self._get_right_control(COMMON_GRID_CONTROL_ID, class_name="CVirtualGridCtrl")

        data = self._wait_clipboard_table(ctrl, copy_each_retry=False)
        if data and "合同编号" not in data.split("\t\r\n", 1)[0].split("\t"):
            # 切页失败时复制到的是其他表格（通常是持仓表），不能当作委托数据返回
            logging.warning("当前委托表格缺少合同编号列，可能未切换到委托页面: header=%s", data.split("\t\r\n", 1)[0])
            return {"code": 1, "status": "failed", "msg": "委托表格缺少合同编号列，客户端可能未切换到委托页面"}
        if data:
            logging.info("当前委托信息获取成功")
            return {
                "code": 0,
                "status": "succeed",
                "data": parse_table(data),
            }
        logging.info("当前委托信息获取失败")
        return {"code": 1, "status": "failed", "msg": "获取当前委托表格数据失败"}

    def get_filled_orders(self):
        logging.info("开始获取历史成交信息...")
        self.switch_to_normal()
        hot_key(["F2"])
        hot_key(["F7"])
        self.refresh()
        ctrl = self._get_right_control(COMMON_GRID_CONTROL_ID, class_name="CVirtualGridCtrl")

        data = self._wait_clipboard_table(ctrl, copy_each_retry=False)
        if data:
            logging.info("历史成交信息获取成功")
            return {
                "code": 0,
                "status": "succeed",
                "data": parse_table(data),
            }
        logging.info("历史成交信息获取失败")
        return {"code": 1, "status": "failed", "msg": "获取当日成交表格数据失败"}

    def sell(self, stock_no, amount, price):
        logging.info(
            f"开始卖出操作，股票代码: {stock_no}, 数量: {amount}, 价格: {price}"
        )
        self.switch_to_normal()
        hot_key(["F2"])
        time.sleep(sleep_time)
        self._fill_trade_form(stock_no, amount, price)
        return self._submit_trade_result("卖出操作成功", "卖出操作失败")

    def buy(self, stock_no, amount, price):
        logging.info(
            f"开始买入操作，股票代码: {stock_no}, 数量: {amount}, 价格: {price}"
        )
        self.switch_to_normal()
        hot_key(["F1"])
        time.sleep(sleep_time)
        self._fill_trade_form(stock_no, amount, price)
        return self._submit_trade_result("买入操作成功", "买入操作失败")

    def sell_kc(self, stock_no, amount, price):
        logging.info(
            f"开始科创板卖出操作，股票代码: {stock_no}, 数量: {amount}, 价格: {price}"
        )
        if self.switch_to_kechuang():
            self.click_kc_sell()
        else:
            hot_key(["F2"])
            time.sleep(sleep_time)
        self._fill_trade_form(stock_no, amount, price)
        return self._submit_trade_result("科创板卖出操作成功", "科创板卖出操作失败")

    def buy_kc(self, stock_no, amount, price):
        logging.info(
            f"开始科创板买入操作，股票代码: {stock_no}, 数量: {amount}, 价格: {price}"
        )
        if self.switch_to_kechuang():
            self.click_kc_buy()
        else:
            hot_key(["F1"])
            time.sleep(sleep_time)
        self._fill_trade_form(stock_no, amount, price)
        return self._submit_trade_result("科创板买入操作成功", "科创板买入操作失败")

    def _select_grid_row_by_keyboard(self, grid_ctrl, row_index: int):
        """使用纯键盘导航选中虚拟表格第 row_index 行，避免硬编码行高像素与滚动条错位。"""
        grid_ctrl.set_focus()
        time.sleep(sleep_time)
        hot_key(["ctrl", "home"])
        time.sleep(sleep_time)
        hot_key(["pgup"])
        time.sleep(sleep_time)
        hot_key(["home"])
        time.sleep(sleep_time)
        for _ in range(row_index):
            hot_key(["down"])
            time.sleep(sleep_time)

    def cancel(self, entrust_no):
        target_no = str(entrust_no or "").strip()
        if not target_no:
            raise ValueError("合同编号 entrust_no 不能为空")
        logging.info(f"开始撤单操作，委托编号: {target_no}")
        self.switch_to_normal()
        hot_key(["F3"])
        self.refresh()
        ctrl = self._get_right_control(COMMON_GRID_CONTROL_ID, class_name="CVirtualGridCtrl")

        data = self._wait_clipboard_table(ctrl, copy_each_retry=False)
        if data:
            entrusts = parse_table(data)
            find = None
            for i, entrust in enumerate(entrusts):
                if str(entrust.get("合同编号", "")).strip() == target_no:
                    find = i
                    break
            if find is None:
                logging.info("未找到指定委托: entrust_no=%s", target_no)
                return {"code": 1, "status": "failed", "msg": "没找到指定订单"}
            logging.info("定位到目标委托行: entrust_no=%s row_index=%d/%d", target_no, find, len(entrusts))
            self._select_grid_row_by_keyboard(ctrl, find)
            result = self._submit_cancel_result(entrust_no=target_no)
            if result.get("code") == 0:
                logging.info("撤单操作成功: entrust_no=%s", target_no)
            else:
                logging.info("撤单操作未确认完成: entrust_no=%s result=%s", target_no, result)
            return result
        logging.info("撤单操作失败: 未能读取撤单列表")
        return {"code": 1, "status": "failed", "msg": "读取可撤委托列表失败"}

    def get_result(self, cid=0x3EC, close_popup=False):
        for popup in self._popup_windows():
            if self._is_persistent_tool_window(popup):
                continue
            text = ""
            try:
                ctrl = self._child_window(
                    popup, control_id=cid, class_name="Static", timeout=0.2
                )
                text = get_text(ctrl).strip()
            except Exception:
                # 兜底识别无“是”按钮、仅含“确定”按钮的本地校验提示弹窗（如 cid=0x410）
                if self._find_yes_button(popup) is None and not self._window_has_static_text(popup, "验证码"):
                    try:
                        alt_ctrl = self._child_window(
                            popup, control_id=0x410, class_name="Static", timeout=0.1
                        )
                        text = get_text(alt_ctrl).strip()
                    except Exception:
                        text = ""
            if not text:
                continue
            if close_popup:
                self._close_result_popup(popup)
            if "已成功提交" in text:
                result = {
                    "code": 0,
                    "status": "succeed",
                    "msg": text,
                }
                if "合同编号：" in text:
                    result["entrust_no"] = text.split("合同编号：", 1)[1].split("。", 1)[0].strip()
                return result
            return {
                "code": 1,
                "status": "failed",
                "msg": text,
            }
        return None

    def refresh(self):
        logging.info("执行刷新操作")
        self.active_main_window()
        hot_key(["F5"])
        time.sleep(refresh_sleep_time)

    def active_main_window(self):
        if self.hwnd_main is None or self.app is None:
            logging.info("激活主窗口跳过: 客户端未绑定")
            return
        try:
            self.main_window = self._main_spec().wrapper_object()
            try:
                self.main_window.restore()
            except Exception:
                pass
            self._ensure_main_window_size()
            self.main_window.set_focus()
            logging.info("主窗口激活成功: hwnd=%s", self.hwnd_main)
            time.sleep(sleep_time)
        except Exception:
            logging.exception("主窗口激活失败，尝试重新绑定")
            self.bind_client()

    def _get_work_area(self):
        if hasattr(ctypes, "windll"):
            try:
                from ctypes import wintypes
                work_rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(work_rect), 0
                ):
                    return work_rect.left, work_rect.top, work_rect.right, work_rect.bottom
            except Exception:
                pass
            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)
            return 0, 0, screen_width, screen_height - TASKBAR_RESERVED_HEIGHT
        return 0, 0, TARGET_MAIN_WINDOW_WIDTH, TARGET_MAIN_WINDOW_HEIGHT

    def _ensure_main_window_size(self):
        try:
            rect = self.main_window.rectangle()
            width, height = rect.width(), rect.height()
            wa_left, wa_top, wa_right, wa_bottom = self._get_work_area()
            wa_width = max(MIN_MAIN_WINDOW_WIDTH, wa_right - wa_left)
            wa_height = max(MIN_MAIN_WINDOW_HEIGHT, wa_bottom - wa_top)

            too_small = width < MIN_MAIN_WINDOW_WIDTH or height < MIN_MAIN_WINDOW_HEIGHT
            out_of_workarea = (
                rect.left < wa_left
                or rect.top < wa_top
                or rect.right > wa_right
                or rect.bottom > wa_bottom
            )
            if not too_small and not out_of_workarea:
                return

            new_width = min(max(width, TARGET_MAIN_WINDOW_WIDTH) if too_small else width, wa_width)
            new_height = min(max(height, TARGET_MAIN_WINDOW_HEIGHT) if too_small else height, wa_height)
            new_x = min(max(rect.left, wa_left), max(wa_left, wa_right - new_width))
            new_y = min(max(rect.top, wa_top), max(wa_top, wa_bottom - new_height))
            if too_small:
                new_x, new_y = wa_left, wa_top

            logging.warning(
                "主窗口尺寸或位置超出工作区，自动调整: 原=(%d,%d,%d,%d) 新=(%d,%d,%dx%d) 工作区=(%d,%d,%d,%d)",
                rect.left,
                rect.top,
                rect.right,
                rect.bottom,
                new_x,
                new_y,
                new_width,
                new_height,
                wa_left,
                wa_top,
                wa_right,
                wa_bottom,
            )
            self.main_window.move_window(x=new_x, y=new_y, width=new_width, height=new_height, repaint=True)
            time.sleep(refresh_sleep_time)
        except Exception:
            logging.exception("检查或调整主窗口尺寸失败")

    def right_click_menu(self, hwnd, x, y, idx=None, key=None):
        control = _resolve_wrapper(hwnd)
        rect = control.rectangle()
        click_x = x if x >= 0 else (rect.right - rect.left) + x
        click_y = y if y >= 0 else (rect.bottom - rect.top) + y
        control.click_input(button="right", coords=(click_x, click_y))
        time.sleep(sleep_time)
        if idx is not None:
            while idx >= 0:
                hot_key(["down_arrow"])
                idx -= 1
            hot_key(["enter"])
        elif key is not None:
            if isinstance(key, list):
                hot_key(key)
            else:
                hot_key([key])

    def switch_to_normal(self):
        logging.info("切换到普通交易标签")
        self.active_main_window()
        hot_key(["esc"])
        tabs = self._find_left_bottom_tabs()
        if tabs is None:
            logging.info("客户端无独立左下角分类标签栏，保持普通/合并交易视图")
            return False
        logging.info("普通交易标签控件: handle=%s", getattr(tabs, "handle", "unknown"))
        tabs.click_input(coords=(10, 5))
        time.sleep(sleep_time)
        return True

    def switch_to_kechuang(self):
        logging.info("切换到科创板标签")
        self.active_main_window()
        tabs = self._find_left_bottom_tabs()
        if tabs is None:
            logging.info("客户端无独立左下角分类标签栏，使用合并交易视图执行科创板委托")
            return False
        logging.info("科创板标签控件: handle=%s", getattr(tabs, "handle", "unknown"))
        tabs.click_input(coords=(200, 5))
        time.sleep(sleep_time)
        return True

    def click_kc_buy(self):
        tree = self.get_tree_hwnd()
        tree.click_input(coords=(10, 10))
        time.sleep(sleep_time)

    def click_kc_sell(self):
        tree = self.get_tree_hwnd()
        tree.click_input(coords=(10, 30))
        time.sleep(sleep_time)

    def copy_table(self, hwnd, ocr_kwargs=None):
        control = _resolve_wrapper(hwnd)
        logging.info("开始复制表格: handle=%s class=%s", getattr(control, "handle", "unknown"), control.class_name())
        self.active_main_window()
        clear_clipboard()
        logging.info("剪贴板已清空，准备发送Ctrl+A Ctrl+C")
        control.set_focus()
        control.type_keys("^A^C", set_foreground=False, pause=sleep_time)
        logging.info("表格复制按键已发送，开始处理验证码")
        self.input_ocr(**(ocr_kwargs or {}))

    def input_ocr(self, **llm_kwargs):
        retry = 0
        while retry < 10:
            logging.info("验证码处理开始: attempt=%d/10", retry + 1)
            dialog = self.get_ocr_hwnd()
            if dialog is None:
                logging.info("当前没有验证码弹窗，结束验证码处理")
                return
            try:
                image, editor, error_label, ok_button = self._get_ocr_controls(dialog)
                if image is None or editor is None:
                    raise RuntimeError("未能定位到验证码图片或输入框")
                logging.info(
                    "验证码控件定位成功: dialog=%s image=%s editor=%s error_label=%s ok_button=%s",
                    getattr(dialog, "handle", "unknown"),
                    getattr(image, "handle", "unknown"),
                    getattr(editor, "handle", "unknown"),
                    getattr(error_label, "handle", "unknown") if error_label is not None else None,
                    getattr(ok_button, "handle", "unknown") if ok_button is not None else None,
                )
            except Exception:
                logging.exception("验证码控件定位失败")
                return

            captcha_image = self.capture_window(image)
            if captcha_image is None:
                logging.info("验证码截图仍为空白，刷新验证码后重试")
                retry += 1
                try:
                    image.click_input()
                    logging.info("验证码图片已点击刷新，准备重试")
                except Exception:
                    logging.exception("验证码图片刷新失败")
                time.sleep(sleep_time)
                continue
            try:
                code = (llm_ocr.ocr_image(captcha_image, **llm_kwargs) or "").strip()
            except Exception as exc:
                logging.exception("验证码OCR调用失败: attempt=%d", retry + 1)
                retry += 1
                if retry >= 10:
                    raise RuntimeError(f"OCR识别请求失败，已重试{retry}次: {exc}")
                try:
                    image.click_input()
                    logging.info("验证码图片已点击刷新，准备重试")
                except Exception:
                    logging.exception("验证码图片刷新失败")
                time.sleep(sleep_time)
                continue
            finally:
                try:
                    captcha_image.close()
                except Exception:
                    logging.exception("释放验证码截图内存失败")

            logging.info("OCR识别结果: %r", code)
            if not code:
                logging.info("OCR识别结果为空，刷新验证码后重试")
                retry += 1
                try:
                    image.click_input()
                except Exception:
                    logging.exception("验证码图片刷新失败")
                time.sleep(sleep_time)
                continue

            submitted = False
            for strategy_name, strategy in self._ocr_input_strategies(editor, code):
                try:
                    self._apply_ocr_input_strategy(editor, code, strategy_name, strategy)
                except Exception:
                    logging.exception(
                        "验证码输入策略执行失败: strategy=%s handle=%s",
                        strategy_name,
                        getattr(editor, "handle", "unknown"),
                    )
                    continue

                self._submit_ocr_dialog(dialog, ok_button)
                logging.info("验证码已提交: strategy=%s", strategy_name)
                status, error_text = self._wait_ocr_submit_result(dialog, error_label=error_label)
                submitted = True
                if status == "closed":
                    logging.info("验证码处理成功: strategy=%s", strategy_name)
                    return
                if status == "error":
                    logging.info("当前验证码提交失败: strategy=%s error=%r", strategy_name, error_text)
                    continue
                logging.info("当前验证码提交结果未明确: strategy=%s，继续尝试下一种输入策略", strategy_name)

            if not submitted:
                logging.info("当前验证码未完成任何有效提交，刷新验证码后重试")
            else:
                logging.info("当前验证码的所有输入策略都已尝试，刷新验证码后重试")
            retry += 1
            try:
                current_dialog = self.get_ocr_hwnd() or dialog
                image, editor, error_label, ok_button = self._get_ocr_controls(current_dialog)
                if image is None:
                    raise RuntimeError("刷新前未定位到验证码图片")
                image.click_input()
                logging.info("验证码图片已点击刷新，进入下一次重试")
            except Exception:
                logging.exception("验证码图片刷新失败")
            time.sleep(sleep_time)

        logging.info("验证码处理失败: 已达到最大重试次数")
        raise RuntimeError("验证码识别失败，已达到最大重试次数，请查看日志排查")

    def capture_window(self, hwnd):
        """截取控件并返回Pillow图片对象，全程不写入工作目录。"""
        control = _resolve_wrapper(hwnd)
        rect = control.rectangle()
        logging.info(
            "开始截图: handle=%s class=%s rect=(%d,%d,%d,%d)",
            getattr(control, "handle", "unknown"),
            _class_name(control),
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
        )

        try:
            image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
            has_content = self._image_has_content(image)
            logging.info("屏幕区域截图完成: has_content=%s", has_content)
            if has_content:
                return image
        except Exception:
            logging.exception("屏幕区域截图失败: handle=%s", getattr(control, "handle", "unknown"))

        try:
            image = control.capture_as_image()
            has_content = self._image_has_content(image)
            logging.info("回退控件截图完成: has_content=%s", has_content)
            return image if has_content else None
        except Exception:
            logging.exception("回退控件截图失败: handle=%s", getattr(control, "handle", "unknown"))
            return None

    @staticmethod
    def _encode_image_base64(image) -> str:
        with BytesIO() as buffer:
            image.save(buffer, format="PNG")
            return b64encode(buffer.getvalue()).decode("utf-8")

    @staticmethod
    def _extract_json_dict(raw_text: str) -> dict:
        if not raw_text:
            raise ValueError("AI返回内容为空")
        text = raw_text.strip()
        fence_pattern = r"`{3}(?:json)?\s*(\{.*?\})\s*`{3}"
        fenced = re.search(fence_pattern, text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        raise ValueError(f"无法从AI响应中解析JSON对象: {raw_text!r}")

    def _get_tree_snapshot(self, max_items=80):
        items = []
        try:
            tree = self.get_tree_hwnd()
            tree_rect = tree.rectangle()
            main_rect = self.main_window.rectangle()
            try:
                roots = tree.roots()
            except Exception:
                roots = []

            def _walk(node, prefix=""):
                if len(items) >= max_items:
                    return
                try:
                    label = (node.text() or "").strip()
                except Exception:
                    label = ""
                path = f"{prefix}\\{label}" if prefix else label
                entry = {"path": path, "text": label}
                try:
                    client_rect = node.client_rect()
                    if client_rect.width() > 0 and client_rect.height() > 0:
                        rel_x = (tree_rect.left - main_rect.left) + (client_rect.left + client_rect.right) // 2
                        rel_y = (tree_rect.top - main_rect.top) + (client_rect.top + client_rect.bottom) // 2
                        entry["center"] = [int(rel_x), int(rel_y)]
                except Exception:
                    pass
                if label:
                    items.append(entry)
                try:
                    children = node.sub_elements()
                except Exception:
                    children = []
                for child in children:
                    if len(items) >= max_items:
                        break
                    _walk(child, path)

            for root in roots:
                _walk(root)
        except Exception:
            logging.debug("提取左侧菜单树快照失败", exc_info=True)
        return items

    def collect_ui_state(self, max_controls=120):
        """收集当前同花顺主窗口、弹窗及主要控件的布局与文本状态，供AI决策。"""
        self._ensure_bound()
        main_rect = self.main_window.rectangle()
        main_left, main_top = main_rect.left, main_rect.top
        main_width, main_height = main_rect.width(), main_rect.height()

        popups_info = []
        for popup in self._popup_windows():
            try:
                p_rect = popup.rectangle()
                children_info = []
                for child in self._dialog_children(popup):
                    c_rect = child.rectangle()
                    rel_left = c_rect.left - main_left
                    rel_top = c_rect.top - main_top
                    rel_right = c_rect.right - main_left
                    rel_bottom = c_rect.bottom - main_top
                    children_info.append(
                        {
                            "handle": getattr(child, "handle", None),
                            "class": _class_name(child),
                            "control_id": _control_id(child),
                            "text": get_text(child).strip(),
                            "rel_rect": [rel_left, rel_top, rel_right, rel_bottom],
                            "center": [(rel_left + rel_right) // 2, (rel_top + rel_bottom) // 2],
                            "screen_center": [
                                (c_rect.left + c_rect.right) // 2,
                                (c_rect.top + c_rect.bottom) // 2,
                            ],
                        }
                    )
                popups_info.append(
                    {
                        "handle": getattr(popup, "handle", None),
                        "title": get_text(popup).strip(),
                        "class": _class_name(popup),
                        "rel_rect": [
                            p_rect.left - main_left,
                            p_rect.top - main_top,
                            p_rect.right - main_left,
                            p_rect.bottom - main_top,
                        ],
                        "static_texts": self._window_static_texts(popup),
                        "button_texts": self._window_button_texts(popup),
                        "controls": children_info,
                    }
                )
            except Exception:
                logging.debug("收集弹窗信息失败", exc_info=True)

        controls_info = []
        important_classes = {
            "Edit",
            "Button",
            "ComboBox",
            "CVirtualGridCtrl",
            "CCustomTabCtrl",
            "SysTabControl32",
            "SysListView32",
            "SysHeader32",
            "Static",
        }
        try:
            for ctrl in self.main_window.descendants():
                if len(controls_info) >= max_controls:
                    break
                try:
                    if not ctrl.is_visible():
                        continue
                except Exception:
                    continue
                cls = _class_name(ctrl)
                text = get_text(ctrl).strip()
                if cls == "Edit" and not text:
                    text = self._editor_value(ctrl)
                if cls not in important_classes and not text:
                    continue
                if cls == "Static" and not text:
                    continue
                try:
                    c_rect = ctrl.rectangle()
                except Exception:
                    continue
                if c_rect.width() <= 1 or c_rect.height() <= 1:
                    continue
                rel_left = c_rect.left - main_left
                rel_top = c_rect.top - main_top
                rel_right = c_rect.right - main_left
                rel_bottom = c_rect.bottom - main_top
                controls_info.append(
                    {
                        "handle": getattr(ctrl, "handle", None),
                        "class": cls,
                        "control_id": _control_id(ctrl),
                        "text": text[:120] if text else "",
                        "rel_rect": [rel_left, rel_top, rel_right, rel_bottom],
                        "center": [(rel_left + rel_right) // 2, (rel_top + rel_bottom) // 2],
                    }
                )
        except Exception:
            logging.debug("收集主窗口控件列表失败", exc_info=True)

        return {
            "main_window": {
                "handle": self.hwnd_main,
                "title": get_text(self.main_window),
                "screen_rect": [main_rect.left, main_rect.top, main_rect.right, main_rect.bottom],
                "size": [main_width, main_height],
            },
            "popups": popups_info,
            "tree_items": self._get_tree_snapshot(),
            "controls": controls_info,
        }

    def _resolve_ai_target_control(self, params: dict, expected_class=None):
        handle = params.get("handle")
        if handle is not None:
            try:
                return self.app.window(handle=int(handle)).wrapper_object()
            except Exception:
                pass
        control_id = params.get("control_id")
        if control_id is not None:
            if isinstance(control_id, str):
                control_id = int(control_id, 16) if control_id.lower().startswith("0x") else int(control_id)
            else:
                control_id = int(control_id)
            for popup in self._popup_windows():
                try:
                    kwargs = {"control_id": control_id, "timeout": 0.5}
                    if expected_class:
                        kwargs["class_name"] = expected_class
                    return self._child_window(popup, **kwargs)
                except Exception:
                    continue
            return self._get_right_control(control_id, class_name=expected_class, timeout=1.5)
        return None

    def _resolve_screen_coords(self, x: int, y: int, coord_type: str = "window"):
        self._ensure_bound()
        if str(coord_type).lower() == "screen":
            return int(x), int(y)
        rect = self.main_window.rectangle()
        return int(rect.left + int(x)), int(rect.top + int(y))

    def _execute_ai_action(self, action_obj: dict, ocr_kwargs: dict = None):
        action = str(action_obj.get("action") or "").strip()
        params = action_obj.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        logging.info("AI执行动作: action=%s params=%s", action, params)

        if self.get_ocr_hwnd() is not None and action not in ("finish", "fail"):
            logging.info("检测到未关闭的验证码弹窗，自动执行验证码识别")
            self.input_ocr(**(ocr_kwargs or {}))

        if action in ("click", "double_click", "right_click"):
            button = "right" if action == "right_click" else str(params.get("button") or "left")
            ctrl = self._resolve_ai_target_control(params)
            if ctrl is not None and ("x" not in params or "y" not in params):
                if action == "double_click":
                    ctrl.double_click_input(button=button)
                else:
                    ctrl.click_input(button=button)
                time.sleep(sleep_time * 2)
                return {"clicked_handle": getattr(ctrl, "handle", None), "action": action}

            if "x" not in params or "y" not in params:
                raise ValueError(f"{action} 需要提供 (x, y) 坐标或有效的 handle/control_id")
            screen_x, screen_y = self._resolve_screen_coords(
                params["x"], params["y"], params.get("coord_type", "window")
            )
            if action == "double_click":
                mouse.double_click(button=button, coords=(screen_x, screen_y))
            else:
                mouse.click(button=button, coords=(screen_x, screen_y))
            time.sleep(sleep_time * 2)
            return {"screen_coords": [screen_x, screen_y], "action": action}

        if action == "select_tree_item":
            tree = self.get_tree_hwnd()
            path = params.get("path")
            text_target = params.get("text")
            if path:
                norm_path = str(path).replace("/", "\\")
                if not norm_path.startswith("\\"):
                    norm_path = "\\" + norm_path
                try:
                    item = tree.get_item(norm_path)
                    item.click_input(double=bool(params.get("double", True)))
                    time.sleep(refresh_sleep_time)
                    return {"selected_path": norm_path}
                except Exception:
                    logging.info("通过路径选择树节点未命中，尝试按名称搜索: path=%s", norm_path)
                    text_target = text_target or norm_path.split("\\")[-1]
            if text_target:
                snapshot = self._get_tree_snapshot(max_items=120)
                for entry in snapshot:
                    if entry.get("text") == text_target or text_target in entry.get("path", ""):
                        if "center" in entry:
                            sx, sy = self._resolve_screen_coords(
                                entry["center"][0], entry["center"][1], "window"
                            )
                            mouse.double_click(coords=(sx, sy))
                            time.sleep(refresh_sleep_time)
                            return {"selected_item": entry}
                raise RuntimeError(f"未在左侧菜单树中找到节点: {text_target!r}")
            raise ValueError("select_tree_item 需要提供 path 或 text 参数")

        if action == "input_text":
            text_val = "" if params.get("text") is None else str(params.get("text"))
            clear_first = bool(params.get("clear", True))
            press_enter = bool(params.get("press_enter", False))
            ctrl = self._resolve_ai_target_control(params, expected_class="Edit") or self._resolve_ai_target_control(params)
            if ctrl is not None:
                if clear_first:
                    self._clear_editor_by_backspace(
                        ctrl, context="AI输入框", steps=max(12, len(text_val) + 4)
                    )
                self._paste_editor_text(ctrl, text_val, context="AI输入框")
                actual = self._editor_value(ctrl)
                if press_enter:
                    hot_key(["enter"])
                return {
                    "handle": getattr(ctrl, "handle", None),
                    "expected": text_val,
                    "actual": actual,
                }
            if "x" in params and "y" in params:
                sx, sy = self._resolve_screen_coords(
                    params["x"], params["y"], params.get("coord_type", "window")
                )
                mouse.click(coords=(sx, sy))
                time.sleep(sleep_time)
                if clear_first:
                    keyboard.send_keys("^a{BACKSPACE}", pause=sleep_time, vk_packet=False)
                if text_val:
                    self._set_clipboard_text(text_val)
                    keyboard.send_keys("^v", pause=sleep_time, vk_packet=False)
                if press_enter:
                    hot_key(["enter"])
                return {"screen_coords": [sx, sy], "input": text_val}
            raise ValueError("input_text 需要提供 handle、control_id 或 (x, y) 坐标")

        if action == "hot_key":
            keys = params.get("keys")
            if isinstance(keys, str):
                keys = [keys]
            if not keys or not isinstance(keys, list):
                raise ValueError("hot_key 需要提供 keys 列表，例如 ['F5'] 或 ['enter']")
            hot_key(keys)
            time.sleep(sleep_time * 2)
            return {"keys": keys}

        if action == "copy_grid":
            ctrl = self._resolve_ai_target_control(params, expected_class="CVirtualGridCtrl")
            if ctrl is None:
                ctrl = self._get_right_control(
                    COMMON_GRID_CONTROL_ID, class_name="CVirtualGridCtrl", timeout=2.0
                )
            raw_data = self._wait_clipboard_table(
                ctrl, copy_each_retry=True, ocr_kwargs=ocr_kwargs
            )
            parsed = parse_table(raw_data) if raw_data else []
            return {
                "handle": getattr(ctrl, "handle", None),
                "rows_count": len(parsed),
                "rows": parsed,
                "raw_preview": (raw_data or "")[:500],
            }

        if action == "get_control_text":
            ctrl = self._resolve_ai_target_control(params)
            if ctrl is None:
                raise ValueError("get_control_text 未找到目标控件")
            return {
                "handle": getattr(ctrl, "handle", None),
                "text": get_text(ctrl),
                "edit_value": self._editor_value(ctrl),
            }

        if action == "scroll":
            sx, sy = self._resolve_screen_coords(
                params.get("x", 400),
                params.get("y", 350),
                params.get("coord_type", "window"),
            )
            wheel_dist = int(params.get("wheel_dist", -3))
            mouse.scroll(coords=(sx, sy), wheel_dist=wheel_dist)
            time.sleep(sleep_time * 2)
            return {"screen_coords": [sx, sy], "wheel_dist": wheel_dist}

        if action == "wait":
            seconds = min(10.0, max(0.05, float(params.get("seconds", 0.5))))
            time.sleep(seconds)
            return {"waited": seconds}

        if action == "switch_tab":
            tab = str(params.get("tab") or "normal").lower()
            if tab in ("kechuang", "kc", "科创板"):
                self.switch_to_kechuang()
            else:
                self.switch_to_normal()
            return {"tab": tab}

        if action == "get_balance":
            return self.get_balance()
        if action == "get_position":
            return self.get_position()
        if action == "get_active_orders":
            return self.get_active_orders()
        if action == "get_filled_orders":
            return self.get_filled_orders()
        if action == "buy":
            price = params.get("price")
            return self.buy(
                stock_no=str(params["stock_no"]),
                amount=int(params["amount"]),
                price=price if price is not None and str(price).strip() != "" else None,
            )
        if action == "sell":
            price = params.get("price")
            return self.sell(
                stock_no=str(params["stock_no"]),
                amount=int(params["amount"]),
                price=price if price is not None and str(price).strip() != "" else None,
            )
        if action == "buy_kc":
            price = params.get("price")
            return self.buy_kc(
                stock_no=str(params["stock_no"]),
                amount=int(params["amount"]),
                price=price if price is not None and str(price).strip() != "" else None,
            )
        if action == "sell_kc":
            price = params.get("price")
            return self.sell_kc(
                stock_no=str(params["stock_no"]),
                amount=int(params["amount"]),
                price=price if price is not None and str(price).strip() != "" else None,
            )
        if action == "cancel":
            return self.cancel(entrust_no=str(params["entrust_no"]))

        raise ValueError(f"不支持的AI动作类型: {action!r}")

    def run_ai_task(
        self,
        prompt: str,
        max_steps: int = 12,
        api_url: str = None,
        api_key: str = None,
        model: str = None,
        proxy: str = None,
        timeout: float = None,
        temperature: float = 0.1,
        max_tokens: int = 1500,
        system_prompt: str = None,
        extra_body: dict = None,
        include_screenshot: bool = False,
    ):
        """使用视觉+控件混合AI Agent循环操作同花顺客户端，不受限制地执行任意功能。"""
        if not prompt or not str(prompt).strip():
            return {"code": 1, "status": "failed", "msg": "prompt 不能为空"}

        max_steps = max(1, min(50, int(max_steps or 12)))
        self.active_main_window()

        ocr_kwargs = {
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "proxy": proxy,
            "timeout": timeout,
        }

        steps_history = []
        last_screenshot_b64 = None
        effective_system_prompt = (
            f"{AI_AGENT_SYSTEM_PROMPT}\n\n【用户附加系统指令】\n{system_prompt}"
            if system_prompt
            else AI_AGENT_SYSTEM_PROMPT
        )

        logging.info(
            "开始执行AI任务: prompt=%r max_steps=%d model=%s api_url=%s",
            prompt,
            max_steps,
            model or "default",
            api_url or "default",
        )

        for step_idx in range(1, max_steps + 1):
            self._ensure_bound()
            ui_state = self.collect_ui_state()
            screenshot = self.capture_window(self.main_window)
            screenshot_b64 = None
            if screenshot is not None:
                try:
                    screenshot_b64 = self._encode_image_base64(screenshot)
                    last_screenshot_b64 = screenshot_b64
                finally:
                    try:
                        screenshot.close()
                    except Exception:
                        pass

            user_context = {
                "task": prompt,
                "current_step": step_idx,
                "max_steps": max_steps,
                "history": steps_history[-8:],
                "ui_state": ui_state,
            }
            user_content = [
                {
                    "type": "text",
                    "text": json.dumps(user_context, ensure_ascii=False),
                }
            ]
            if screenshot_b64:
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                    }
                )

            messages = [
                {"role": "system", "content": effective_system_prompt},
                {"role": "user", "content": user_content},
            ]

            raw_reply = llm_ocr.request_chat_completion(
                messages=messages,
                api_url=api_url,
                api_key=api_key,
                model=model,
                proxy=proxy,
                timeout=timeout,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
            logging.info("AI第%d步原始响应: %s", step_idx, raw_reply)

            decision = self._extract_json_dict(raw_reply)
            action = str(decision.get("action") or "").strip()
            thought = decision.get("thought") or ""

            if action == "finish":
                response = {
                    "code": 0,
                    "status": "succeed",
                    "msg": decision.get("message") or "AI任务执行完成",
                    "data": decision.get("result"),
                    "steps": steps_history
                    + [
                        {
                            "step": step_idx,
                            "thought": thought,
                            "action": "finish",
                        }
                    ],
                }
                if include_screenshot and last_screenshot_b64:
                    response["screenshot_base64"] = last_screenshot_b64
                return response

            if action == "fail":
                response = {
                    "code": 1,
                    "status": "failed",
                    "msg": decision.get("message") or "AI判定任务无法完成",
                    "data": decision.get("result"),
                    "steps": steps_history
                    + [
                        {
                            "step": step_idx,
                            "thought": thought,
                            "action": "fail",
                        }
                    ],
                }
                if include_screenshot and last_screenshot_b64:
                    response["screenshot_base64"] = last_screenshot_b64
                return response

            step_record = {
                "step": step_idx,
                "thought": thought,
                "action": action,
                "params": decision.get("params") or {},
            }
            try:
                observation = self._execute_ai_action(decision, ocr_kwargs=ocr_kwargs)
                step_record["status"] = "ok"
                step_record["observation"] = observation
            except Exception as exc:
                logging.exception("AI第%d步动作执行异常: action=%s", step_idx, action)
                step_record["status"] = "error"
                step_record["error"] = str(exc)

            steps_history.append(step_record)

        response = {
            "code": 1,
            "status": "failed",
            "msg": f"已达到最大步骤数({max_steps})，任务仍未结束",
            "steps": steps_history,
        }
        if include_screenshot and last_screenshot_b64:
            response["screenshot_base64"] = last_screenshot_b64
        return response

    def test(self):
        pass
