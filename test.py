from pywinauto import Application
from pywinauto.findwindows import ElementNotFoundError

WINDOW_TITLE = "网上股票交易系统5.0"

def connect_to_app():
    try:
        app = Application(backend="uia").connect(title=WINDOW_TITLE)
        main_window = app.window(title=WINDOW_TITLE)
        if not main_window.is_visible():
            raise ValueError("窗口不可见。请确保窗口未最小化。")
        return main_window
    except ElementNotFoundError:
        raise ValueError(f"无法找到标题为 '{WINDOW_TITLE}' 的窗口。请确保应用程序已经运行。")

def find_tree_view(main_window):
    try:
        return main_window.child_window(control_type="Tree")
    except ElementNotFoundError:
        raise ValueError("无法找到TreeView控件")

def print_tree_structure(tree_item, level=0):
    try:
        for child in tree_item.children():
            print("  " * level + child.window_text())
            print_tree_structure(child, level + 1)
    except AttributeError:
        pass

def main():
    try:
        main_window = connect_to_app()
        print(f"成功找到并连接到窗口：'{WINDOW_TITLE}'")

        tree_view = find_tree_view(main_window)
        print("成功找到TreeView控件")

        print("TreeView的结构：")
        print_tree_structure(tree_view)

    except ValueError as e:
        print(str(e))

if __name__ == "__main__":
    main()
