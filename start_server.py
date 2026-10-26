"""thsauto 启动脚本，由各虚拟机桌面上的 start_ths_server.bat 调用。

桌面脚本只负责把 GitHub 上的最新代码下载并解压到 %THSAUTO_HOME%\\download，
本脚本负责用新代码替换代码目录、设置运行参数并启动 server.py。
这里的启动逻辑修改后推送到 GitHub，各虚拟机下次启动时即可生效。

可通过环境变量覆盖默认值：
    THSAUTO_HOME      运行目录，默认 %USERPROFILE%\\thsauto
    THSAUTO_HOST      监听地址，默认 0.0.0.0
    THSAUTO_PORT      监听端口，默认 6003
    THS_CLIENT_PATH   同花顺客户端路径，默认 %USERPROFILE%\\Desktop\\同花顺\\xiadan.exe
    THSAUTO_LOG_FILE  日志文件，默认 %THSAUTO_HOME%\\thsauto.log
"""
import os
import shutil
import subprocess
import sys

THSAUTO_HOME = os.path.abspath(
    os.environ.get("THSAUTO_HOME") or os.path.join(os.path.expanduser("~"), "thsauto")
)
CODE_DIR = os.path.join(THSAUTO_HOME, "code")
OLD_CODE_DIR = os.path.join(THSAUTO_HOME, "code_old")
# 桌面脚本把 GitHub 的 master 分支压缩包解压到 download 目录，压缩包内的顶层目录固定为 thsauto-master
DOWNLOADED_CODE_DIR = os.path.join(THSAUTO_HOME, "download", "thsauto-master")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = "6003"
DEFAULT_CLIENT_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "同花顺", "xiadan.exe")
# 新代码必须包含这些文件才会替换旧代码，防止解压不完整时把能用的旧代码换掉
REQUIRED_FILES = ("server.py", "thsauto.py", "llm_ocr.py", "const.py")


def _log(message):
    print(message, flush=True)


def _same_path(path_a, path_b):
    return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(os.path.abspath(path_b))


def _is_complete(code_dir):
    return all(os.path.isfile(os.path.join(code_dir, name)) for name in REQUIRED_FILES)


def install_downloaded_code():
    """用新下载的代码替换代码目录：旧代码先挪到 code_old，新代码就位失败时再挪回来。"""
    if not _is_complete(DOWNLOADED_CODE_DIR):
        _log("新下载的代码不完整，继续使用上次下载的代码")
        return False
    try:
        if os.path.exists(OLD_CODE_DIR):
            shutil.rmtree(OLD_CODE_DIR)
        if os.path.exists(CODE_DIR):
            os.rename(CODE_DIR, OLD_CODE_DIR)
    except OSError as exc:
        _log("挪走旧代码失败，继续使用上次下载的代码: %s" % exc)
        return False
    try:
        os.rename(DOWNLOADED_CODE_DIR, CODE_DIR)
    except OSError as exc:
        _log("新代码就位失败，恢复上次下载的代码: %s" % exc)
        try:
            if os.path.exists(OLD_CODE_DIR) and not os.path.exists(CODE_DIR):
                os.rename(OLD_CODE_DIR, CODE_DIR)
        except OSError as restore_exc:
            _log("恢复旧代码失败: %s" % restore_exc)
        return False
    _log("代码已更新为 GitHub 最新版本")
    return True


def resolve_code_dir():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not _same_path(script_dir, DOWNLOADED_CODE_DIR):
        # 从代码目录运行（下载失败）或手动在其他目录调试时，不移动任何目录
        return script_dir
    install_downloaded_code()
    if _is_complete(CODE_DIR):
        return CODE_DIR
    # 代码目录不可用（如首次部署时替换失败），直接用新下载的代码启动
    return script_dir


def main():
    os.makedirs(THSAUTO_HOME, exist_ok=True)
    code_dir = resolve_code_dir()
    server_path = os.path.join(code_dir, "server.py")
    if not os.path.isfile(server_path):
        _log("本地没有可用的 thsauto 代码，无法启动服务: %s" % server_path)
        return 1

    env = os.environ.copy()
    env["THSAUTO_HOME"] = THSAUTO_HOME
    env.setdefault("THSAUTO_LOG_FILE", os.path.join(THSAUTO_HOME, "thsauto.log"))
    host = env.get("THSAUTO_HOST") or DEFAULT_HOST
    port = env.get("THSAUTO_PORT") or DEFAULT_PORT
    client_path = env.get("THS_CLIENT_PATH") or DEFAULT_CLIENT_PATH
    _log(
        "启动 thsauto 服务: 代码目录=%s 监听=%s:%s 客户端=%s 日志=%s"
        % (code_dir, host, port, client_path, env["THSAUTO_LOG_FILE"])
    )
    # 工作目录放在 THSAUTO_HOME 而不是代码目录，避免占用代码目录导致下次启动无法替换
    try:
        return subprocess.call(
            [sys.executable, server_path, host, port, client_path],
            cwd=THSAUTO_HOME,
            env=env,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
