#!/usr/bin/env python3
"""通过 govc 重启 Windows 虚拟机桌面的 start_ths_server.bat，默认排除 Forex、书宾。

仅依赖 Python 标准库、govc 和 git。在已配置 GOVC_URL 等环境变量的管理机运行。
先预览：python3 restart_ths_vms.py --dry-run
执行：  python3 restart_ths_vms.py
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from typing import Optional
from urllib.parse import urlparse
import urllib.request


DEFAULT_ESXI_HOSTS = ("192.168.2.68", "192.168.2.66")
DEFAULT_EXCLUDES = ("forex", "书宾")
RUNTIME_FILES = ("server.py", "thsauto.py", "llm_ocr.py", "const.py", "start_server.py")

# 此段在 Windows 已登录的桌面会话中执行，必须兼容 Python 3.8。
GUEST_SOURCE = r'''
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import traceback
import urllib.request


def listener_pids(port):
    text = subprocess.check_output(['netstat', '-ano', '-p', 'tcp']).decode('ascii', errors='ignore')
    return sorted({int(parts[-1]) for parts in (line.split() for line in text.splitlines())
                   if len(parts) >= 5 and parts[1].endswith(':' + str(port)) and parts[3] == 'LISTENING'})


def run_guest(config):
    report = {'vm_id': config['vm_id'], 'phase': 'preparing'}

    def send_report():
        request = urllib.request.Request(config['report_url'],
            data=json.dumps(report).encode('utf-8'), headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(request, timeout=10).read()

    try:
        text = subprocess.check_output(['ipconfig']).decode('ascii', errors='ignore')
        if config['ip'] not in re.findall(r'\b\d+\.\d+\.\d+\.\d+\b', text):
            raise RuntimeError('Guest IP does not match selected VM; no process was stopped')
        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
        desktop = Path(buffer.value) if result == 0 else Path.home() / 'Desktop'
        batch = desktop / 'start_ths_server.bat'
        if not batch.is_file():
            raise RuntimeError('Desktop start_ths_server.bat was not found; no process was stopped')
        report['batch_path'] = str(batch)
        pids = listener_pids(config['port'])
        for pid in pids:
            text = subprocess.check_output(['tasklist', '/FI', 'PID eq ' + str(pid), '/FO', 'CSV', '/NH']).decode('mbcs')
            rows = list(csv.reader(text.splitlines()))
            if not rows or rows[0][0].lower() not in ('python.exe', 'pythonw.exe', 'py.exe'):
                raise RuntimeError('Service port is occupied by a non-Python process; no process was stopped')
        report['stopped_pids'] = pids
        send_report()
        for pid in pids:
            subprocess.check_call(['taskkill', '/PID', str(pid), '/F'])
        os.startfile(str(batch))
        report['phase'] = 'starting'
        send_report()
        home = Path(os.environ.get('THSAUTO_HOME') or str(Path.home() / 'thsauto'))
        deadline = time.monotonic() + config['timeout']
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', config['port']), timeout=1):
                    pass
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('Service did not open its listening port before timeout')
        report['new_pids'] = listener_pids(config['port'])
        hashes = {}
        for name in config['expected_files']:
            path = home / 'code' / name
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        report['file_hashes'] = hashes
        report['phase'] = 'ready'
    except Exception:
        report['phase'] = 'failed'
        report['error'] = traceback.format_exc()
    try:
        send_report()
    except Exception:
        pass
'''


def replace_govc_host(govc_url, host):
    parsed = urlparse(govc_url)
    if not parsed.scheme or not parsed.netloc:
        return govc_url
    auth, sep, hostport = parsed.netloc.rpartition("@")
    _, colon, port = hostport.rpartition(":")
    new_hostport = f"{host}:{port}" if colon and "]" not in port else host
    return parsed._replace(netloc=f"{auth}{sep}{new_hostport}").geturl()


def resolve_govc_targets(esxi_hosts=(), environ=None):
    env = os.environ if environ is None else environ
    govc_url = env.get("GOVC_URL", "").strip()
    if esxi_hosts:
        if govc_url:
            return [(host, replace_govc_host(govc_url, host)) for host in esxi_hosts]
        return [(host, None) for host in esxi_hosts]
    if govc_url:
        parsed_host = urlparse(govc_url).hostname or ""
        if parsed_host in DEFAULT_ESXI_HOSTS:
            return [(host, replace_govc_host(govc_url, host)) for host in DEFAULT_ESXI_HOSTS]
        return [(parsed_host, govc_url)]
    return [("", None)]


def govc_env(govc_url=None, esxi_host=""):
    if govc_url is None and not esxi_host:
        return None
    env = os.environ.copy()
    if govc_url:
        env["GOVC_URL"] = govc_url
    elif esxi_host:
        env["GOVC_HOST"] = esxi_host
    return env


def command(args, timeout=30, env=None):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{args[0]} 执行超时") from None
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{args[0]} 退出码 {result.returncode}")
    return result.stdout


def select_vms(inventory, names=(), excludes=()):
    windows = [vm for vm in inventory if (vm.get("config", {}).get("guestId") or "").startswith("windows")]
    excluded = DEFAULT_EXCLUDES + tuple(text.casefold() for text in excludes)
    if names:
        known = {vm["name"] for vm in windows}
        missing = set(names) - known
        if missing:
            raise ValueError("未找到 Windows 虚拟机：" + "、".join(sorted(missing)))
        windows = [vm for vm in windows if vm["name"] in names]
        blocked = [vm["name"] for vm in windows if any(text in vm["name"].casefold() for text in excluded)]
        if blocked:
            raise ValueError("虚拟机在排除名单内：" + "、".join(blocked))
    return [vm for vm in windows if not any(text in vm["name"].casefold() for text in excluded)]


def latest_version():
    repo = str(Path(__file__).resolve().parent)
    command(["git", "-C", repo, "fetch", "--no-tags", "origin", "master"], timeout=60)
    revision = command(["git", "-C", repo, "rev-parse", "FETCH_HEAD"]).strip()
    expected = {}
    for name in RUNTIME_FILES:
        # 保留原始换行，确保与 GitHub 压缩包中的文件逐字节比较。
        result = subprocess.run(["git", "-C", repo, "show", revision + ":" + name], capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("无法读取远端版本中的 " + name)
        expected[name] = hashlib.sha256(result.stdout).hexdigest()
    return revision, expected


def local_address(guest_ip):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.connect((guest_ip, 6003))
        return connection.getsockname()[0]


@dataclass
class Job:
    vm_id: str
    name: str
    ip: str
    esxi_host: str = ""
    govc_url: Optional[str] = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    delivered: bool = False
    report: dict = field(default_factory=dict)


class CallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, jobs, service_port, timeout, expected):
        self.jobs = {job.token: job for job in jobs}
        self.condition = threading.Condition()
        self.stop_requested = threading.Event()
        self.service_port = service_port
        self.start_timeout = timeout
        self.expected = expected
        super().__init__(address, CallbackHandler)

    def wait_for(self, predicate, timeout):
        with self.condition:
            self.condition.wait_for(lambda: predicate() or self.stop_requested.is_set(), timeout=timeout)
            if self.stop_requested.is_set():
                raise RuntimeError("批量重启已中断")
            return predicate()


class CallbackHandler(BaseHTTPRequestHandler):
    def job_for(self, prefix):
        path = urlparse(self.path).path
        token = path[len(prefix):] if path.startswith(prefix) else ""
        job = self.server.jobs.get(token)
        if job is None or self.client_address[0] != job.ip:
            self.send_error(403)
            return None
        return job

    def do_GET(self):
        job = self.job_for("/restart/")
        if job is None:
            return
        with self.server.condition:
            if job.delivered:
                # 重试只处理命令未执行的情况，同一台机器的一次任务只执行一次。
                self.send_error(409)
                return
            job.delivered = True
            self.server.condition.notify_all()
        host, port = self.server.server_address
        config = {"vm_id": job.vm_id, "ip": job.ip, "port": self.server.service_port,
                  "timeout": self.server.start_timeout, "expected_files": self.server.expected,
                  "report_url": f"http://{host}:{port}/report/{job.token}"}
        body = (GUEST_SOURCE + "\nrun_guest(" + repr(config) + ")\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        job = self.job_for("/report/")
        if job is None:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                raise ValueError("invalid body size")
            report = json.loads(self.rfile.read(length))
            if report.get("vm_id") != job.vm_id or report.get("phase") not in ("preparing", "starting", "ready", "failed"):
                raise ValueError("invalid report")
        except (ValueError, TypeError, AttributeError):
            self.send_error(400)
            return
        with self.server.condition:
            if job.report.get("phase") not in ("ready", "failed"):
                job.report = report
            self.server.condition.notify_all()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def send_restart_command(job, url, stop_requested):
    base = ["govc", "vm.keystrokes", "-vm", job.name]
    text = 'cmd /c py -c "import urllib.request as u;exec(u.urlopen(\'' + url + '\',timeout=15).read())"'
    env = govc_env(job.govc_url, job.esxi_host)
    for args, pause in ((["-lg", "-c", "KEY_R"], 0.5), (["-s", text], 0.2), (["-c", "KEY_ENTER"], 0)):
        if stop_requested.is_set():
            raise RuntimeError("批量重启已中断")
        if env is not None:
            command(base + args, timeout=10, env=env)
        else:
            command(base + args, timeout=10)
        if pause:
            stop_requested.wait(pause)


def restart_one(job, server):
    result = {"name": job.name, "ip": job.ip, "ok": False}
    if job.esxi_host:
        result["esxi_host"] = job.esxi_host
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/restart/{job.token}"
        for attempt in range(2):
            if server.stop_requested.is_set():
                raise RuntimeError("批量重启已中断")
            if not job.delivered:
                send_restart_command(job, url, server.stop_requested)
            if server.wait_for(lambda: job.delivered, timeout=8):
                break
        else:
            raise RuntimeError("桌面启动命令未执行：请检查桌面会话、py 命令和虚拟机到管理机的 HTTP 连接")
        if not server.wait_for(lambda: job.report.get("phase") in ("ready", "failed"), server.start_timeout + 30):
            raise RuntimeError("等待虚拟机回传结果超时；请查看该虚拟机桌面")
        with server.condition:
            report = dict(job.report)
        if report["phase"] == "failed":
            raise RuntimeError(report.get("error", "虚拟机执行失败"))
        if not report.get("new_pids"):
            raise RuntimeError("服务端口没有监听进程")
        if server.expected and report.get("file_hashes") != server.expected:
            changed = [name for name, digest in server.expected.items() if report.get("file_hashes", {}).get(name) != digest]
            raise RuntimeError("服务已启动，但未加载预期的最新代码：" + "、".join(changed))
        request = urllib.request.Request(f"http://{job.ip}:{server.service_port}/thsauto/balance", method="OPTIONS")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200 or "GET" not in (response.headers.get("Allow") or ""):
                raise RuntimeError("HTTP 接口检查未通过")
        result.update(ok=True, stopped_pids=report.get("stopped_pids", []), new_pids=report["new_pids"], version_checked=bool(server.expected))
    except Exception as exc:
        result["error"] = str(exc)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="只列出目标虚拟机，不发送按键或重启服务")
    parser.add_argument("--esxi-host", action="append", default=[], metavar="HOST",
                        help=f"指定 ESXi 主机地址，可重复指定；GOVC_URL 指向默认主机时会自动同时管理 {' 和 '.join(DEFAULT_ESXI_HOSTS)}")
    parser.add_argument("--vm", action="append", default=[], metavar="NAME", help="只重启这个完整名称的虚拟机，可重复指定")
    parser.add_argument("--exclude", action="append", default=[], metavar="TEXT", help="额外排除名称包含此文字的虚拟机，不区分大小写；始终排除 Forex、书宾")
    parser.add_argument("--jobs", type=int, default=3, help="同时处理的虚拟机数，默认 3")
    parser.add_argument("--timeout", type=int, default=180, help="每台服务启动的等待秒数，默认 180")
    parser.add_argument("--host", help="虚拟机可访问的管理机 IPv4 地址，默认自动判断")
    parser.add_argument("--helper-port", type=int, default=18769, help="管理机临时 HTTP 回传端口，默认 18769")
    parser.add_argument("--port", type=int, default=6003, help="Windows 上 thsauto 服务的端口，默认 6003，须与桌面脚本一致")
    parser.add_argument("--no-version-check", action="store_true", help="跳过从 origin/master 获取最新代码并比较文件，无需 git")
    parser.add_argument("--report", type=Path, metavar="PATH", help="将逐台结果保存为 JSON 文件")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout < 1 or not 1 <= args.port <= 65535 or not 0 <= args.helper_port <= 65535:
        parser.error("并行数和超时必须为正数，端口必须在有效范围内（helper-port 可为 0 自动分配）")
    if any(not text for text in args.exclude):
        parser.error("--exclude 不能为空")
    return args


def main(argv=None):
    args = parse_args(argv)
    if not shutil.which("govc"):
        raise RuntimeError("未找到 govc，请先安装并配置 GOVC_URL 等环境变量")
    inventory = []
    for esxi_host, govc_url in resolve_govc_targets(args.esxi_host):
        env = govc_env(govc_url, esxi_host)
        output = command(["govc", "vm.info", "-json", "*"], timeout=30, env=env) if env is not None else command(["govc", "vm.info", "-json", "*"], timeout=30)
        for vm in json.loads(output).get("virtualMachines", []):
            vm = dict(vm)
            vm["_esxi_host"] = esxi_host
            vm["_govc_url"] = govc_url
            inventory.append(vm)
    selected = select_vms(inventory, args.vm, args.exclude)
    if not selected:
        raise RuntimeError("没有匹配的 Windows 虚拟机（默认排除 Forex、书宾）")
    jobs, unavailable = [], []
    for vm in selected:
        guest = vm.get("guest", {})
        reason = None
        if vm.get("runtime", {}).get("powerState") != "poweredOn":
            reason = "虚拟机未开机"
        elif guest.get("toolsRunningStatus") != "guestToolsRunning":
            reason = "VMware Tools 未运行"
        else:
            try:
                ipaddress.IPv4Address(guest.get("ipAddress", ""))
            except ipaddress.AddressValueError:
                reason = "没有可用的 IPv4 地址"
        if reason:
            entry = {"name": vm["name"], "ok": False, "error": reason}
            if vm.get("_esxi_host"):
                entry["esxi_host"] = vm["_esxi_host"]
            unavailable.append(entry)
            print(f"[无法处理] {vm['name']}：{reason}", flush=True)
        else:
            job = Job(
                str(vm["self"]["value"]),
                vm["name"],
                guest["ipAddress"],
                esxi_host=vm.get("_esxi_host", ""),
                govc_url=vm.get("_govc_url"),
            )
            jobs.append(job)
            host_label = f" @{job.esxi_host}" if job.esxi_host else ""
            print(f"[目标] {job.name} ({job.ip}{host_label})", flush=True)
    if args.dry_run:
        print(f"预览：可重启 {len(jobs)} 台，无法处理 {len(unavailable)} 台；Forex、书宾 已排除。")
        return 1 if unavailable else 0
    if not jobs:
        return 1
    host = args.host or local_address(jobs[0].ip)
    address = ipaddress.IPv4Address(host)
    if address.is_loopback or address.is_unspecified:
        raise ValueError("--host 必须是虚拟机可访问的管理机 IPv4 地址")
    revision, expected = (None, {}) if args.no_version_check else latest_version()
    print(f"开始重启 {len(jobs)} 台；目标版本：{revision or '不检查版本'}", flush=True)
    results = list(unavailable)
    with CallbackServer((host, args.helper_port), jobs, args.port, args.timeout, expected) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        executor = ThreadPoolExecutor(max_workers=args.jobs)
        futures = []
        try:
            futures = [executor.submit(restart_one, job, server) for job in jobs]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                message = "重启完成，HTTP 检查通过" if result["ok"] else result["error"]
                print(f"[{'成功' if result['ok'] else '失败'}] {result['name']}：{message}", flush=True)
        finally:
            server.stop_requested.set()
            with server.condition:
                server.condition.notify_all()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True)
            server.shutdown()
            thread.join()
    results.sort(key=lambda item: item["name"])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"expected_commit": revision, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    succeeded = sum(result["ok"] for result in results)
    print(f"完成：成功 {succeeded} 台，失败 {len(results) - succeeded} 台。", flush=True)
    return 0 if succeeded == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断；已发送到虚拟机的重启任务可能仍在执行。", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(1)
