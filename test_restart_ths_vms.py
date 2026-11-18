import ast
from contextlib import nullcontext, redirect_stdout
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

import restart_ths_vms as restart


def vm(name, guest_id="windows7_64Guest"):
    return {"name": name, "self": {"value": name}, "config": {"guestId": guest_id},
            "runtime": {"powerState": "poweredOn"},
            "guest": {"ipAddress": "192.0.2.10", "toolsRunningStatus": "guestToolsRunning"}}


class SelectionTests(unittest.TestCase):
    def test_excludes_forex_and_linux(self):
        inventory = [
            vm("Windows 7 - 爸爸"),
            vm("Windows 7 - FOREX"),
            vm("Windows 7 - 书宾"),
            vm("Debian", "debian10_64Guest"),
        ]
        self.assertEqual([inventory[0]], restart.select_vms(inventory))

    def test_exact_vm_selection_does_not_match_similar_name(self):
        inventory = [vm("Windows 7 - 王道远"), vm("Windows 7 - 王道远（国金）")]
        self.assertEqual([inventory[0]], restart.select_vms(inventory, ["Windows 7 - 王道远"]))

    def test_explicit_forex_and_unknown_vm_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "排除名单"):
            restart.select_vms([vm("Windows 7 - Forex")], ["Windows 7 - Forex"])
        with self.assertRaisesRegex(ValueError, "排除名单"):
            restart.select_vms([vm("Windows 7 - 书宾")], ["Windows 7 - 书宾"])
        with self.assertRaisesRegex(ValueError, "未找到"):
            restart.select_vms([vm("Windows 7 - 爸爸")], ["typo"])

    def test_dry_run_does_not_create_helper_or_send_commands(self):
        with mock.patch.object(restart.shutil, "which", return_value="govc"), \
                mock.patch.object(restart, "command", return_value=json.dumps({"virtualMachines": [vm("Windows 7 - 爸爸")]})), \
                mock.patch.object(restart, "latest_version") as version, \
                mock.patch.object(restart, "CallbackServer") as helper, \
                mock.patch.object(restart, "send_restart_command") as send, redirect_stdout(StringIO()):
            self.assertEqual(0, restart.main(["--dry-run"]))
        version.assert_not_called()
        helper.assert_not_called()
        send.assert_not_called()

    def test_resolves_both_default_esxi_hosts_from_govc_url(self):
        targets = restart.resolve_govc_targets(environ={"GOVC_URL": "https://root:secret@192.168.2.68"})
        self.assertEqual([
            ("192.168.2.68", "https://root:secret@192.168.2.68"),
            ("192.168.2.66", "https://root:secret@192.168.2.66"),
        ], targets)

    def test_send_restart_command_uses_job_govc_url(self):
        job = restart.Job("6", "Windows 7 - 吴鹏飞", "192.168.2.238", esxi_host="192.168.2.66", govc_url="https://root:secret@192.168.2.66")
        seen_urls = []

        def fake_command(args, timeout=30, env=None):
            seen_urls.append(env["GOVC_URL"])
            return ""

        with mock.patch.object(restart, "command", side_effect=fake_command):
            restart.send_restart_command(job, "http://192.168.2.1:18769/restart/tok", threading.Event())
        self.assertEqual(["https://root:secret@192.168.2.66"] * 3, seen_urls)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.job = restart.Job("3", "Windows 7 - 爸爸", "127.0.0.1")
        self.expected = {"thsauto.py": "expected-digest"}
        self.server = restart.CallbackServer(("127.0.0.1", 0), [self.job], 0, 1, self.expected)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service_requests = []
        requests = self.service_requests

        class ServiceHandler(BaseHTTPRequestHandler):
            def do_OPTIONS(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Allow", "GET, OPTIONS, HEAD")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.service = ThreadingHTTPServer(("127.0.0.1", 0), ServiceHandler)
        self.service_thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.service_thread.start()
        self.server.service_port = self.service.server_address[1]
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        for server, thread in ((self.server, self.thread), (self.service, self.service_thread)):
            server.shutdown()
            server.server_close()
            thread.join()

    def post(self, report):
        request = urllib.request.Request(self.base + "/report/" + self.job.token, data=json.dumps(report).encode())
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read()

    def deliver(self, job, url, stop_requested):
        with urllib.request.urlopen(url, timeout=2) as response:
            source = response.read().decode()
        ast.parse(source, feature_version=(3, 8))
        self.post({"vm_id": job.vm_id, "phase": "ready", "new_pids": [22], "stopped_pids": [11], "file_hashes": self.expected})

    def test_payload_is_delivered_only_once(self):
        url = self.base + "/restart/" + self.job.token
        with urllib.request.urlopen(url, timeout=2) as response:
            self.assertIn("run_guest(", response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url, timeout=2)
        self.assertEqual(409, error.exception.code)
        error.exception.close()

    def test_invalid_token_and_wrong_vm_report_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/restart/invalid", timeout=2)
        self.assertEqual(403, error.exception.code)
        error.exception.close()
        self.assertFalse(self.job.delivered)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post({"vm_id": "wrong-vm", "phase": "ready"})
        self.assertEqual(400, error.exception.code)
        error.exception.close()
        self.assertEqual({}, self.job.report)

    def test_completed_report_cannot_be_overwritten_by_delayed_progress(self):
        self.post({"vm_id": "3", "phase": "ready"})
        self.post({"vm_id": "3", "phase": "starting"})
        self.assertEqual("ready", self.job.report["phase"])

    def test_success_requires_guest_report_version_and_http_check(self):
        with mock.patch.object(restart, "send_restart_command", side_effect=self.deliver) as send:
            result = restart.restart_one(self.job, self.server)
        self.assertTrue(result["ok"], result)
        self.assertEqual([22], result["new_pids"])
        send.assert_called_once()
        self.assertEqual(["/thsauto/balance"], self.service_requests)

    def test_retries_when_first_desktop_command_is_not_delivered(self):
        attempts = []

        def send(*args):
            attempts.append(1)
            if len(attempts) == 2:
                self.deliver(*args)

        wait = self.server.wait_for
        with mock.patch.object(restart, "send_restart_command", side_effect=send), \
                mock.patch.object(self.server, "wait_for", side_effect=lambda predicate, timeout: wait(predicate, 0.02)):
            result = restart.restart_one(self.job, self.server)
        self.assertTrue(result["ok"], result)
        self.assertEqual(2, len(attempts))

    def test_old_code_is_reported_as_failure(self):
        def deliver_old_code(job, url, stop_requested):
            with urllib.request.urlopen(url, timeout=2) as response:
                response.read()
            self.post({"vm_id": job.vm_id, "phase": "ready", "new_pids": [22], "file_hashes": {"thsauto.py": "old"}})

        with mock.patch.object(restart, "send_restart_command", side_effect=deliver_old_code):
            result = restart.restart_one(self.job, self.server)
        self.assertFalse(result["ok"])
        self.assertIn("未加载预期的最新代码", result["error"])
        self.assertEqual([], self.service_requests)

    def test_cancellation_does_not_send_more_desktop_commands(self):
        self.server.stop_requested.set()
        with mock.patch.object(restart, "send_restart_command") as send:
            result = restart.restart_one(self.job, self.server)
        self.assertFalse(result["ok"])
        send.assert_not_called()


class GuestRestartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.desktop = self.home / "Desktop"
        self.desktop.mkdir()
        self.batch = self.desktop / "start_ths_server.bat"
        self.batch.write_text("@echo off")
        (self.home / "code").mkdir()
        (self.home / "code" / "thsauto.py").write_bytes(b"# expected version\n")
        self.expected = {"thsauto.py": hashlib.sha256(b"# expected version\n").hexdigest()}
        self.config = {"vm_id": "3", "ip": "192.0.2.10", "port": 6003, "timeout": 1,
                       "expected_files": self.expected, "report_url": "http://127.0.0.1/report"}
        self.namespace = {}
        exec(compile(restart.GUEST_SOURCE, "<guest>", "exec"), self.namespace)
        self.reports = []
        self.task_image = "python.exe"
        self.guest_ip = "192.0.2.10"
        self.kill = mock.Mock()
        self.start = mock.Mock()

        def desktop_path(hwnd, folder, token, flags, buffer):
            buffer.value = str(self.desktop)
            return 0

        def output(args):
            text = self.guest_ip if args[0] == "ipconfig" else '"' + self.task_image + '","11"'
            return SimpleNamespace(decode=lambda *args, **kwargs: text)

        def send(request, timeout):
            self.reports.append(json.loads(request.data))
            return SimpleNamespace(read=lambda: b"OK")

        self.namespace.update({
            "ctypes": SimpleNamespace(create_unicode_buffer=ctypes.create_unicode_buffer,
                                      windll=SimpleNamespace(shell32=SimpleNamespace(SHGetFolderPathW=desktop_path))),
            "subprocess": SimpleNamespace(check_output=output, check_call=self.kill),
            "os": SimpleNamespace(environ={"THSAUTO_HOME": str(self.home)}, startfile=self.start),
            "socket": SimpleNamespace(create_connection=lambda *args, **kwargs: nullcontext()),
            "urllib": SimpleNamespace(request=SimpleNamespace(Request=urllib.request.Request, urlopen=send)),
            "listener_pids": mock.Mock(side_effect=[[11], [22]]),
        })

    def run_guest(self):
        self.namespace["run_guest"](self.config)
        return self.reports[-1]

    def test_wrong_guest_ip_does_not_stop_or_start_processes(self):
        self.guest_ip = "192.0.2.20"
        self.assertEqual("failed", self.run_guest()["phase"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_missing_desktop_script_does_not_stop_processes(self):
        self.batch.unlink()
        self.assertIn("was not found", self.run_guest()["error"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_non_python_listener_is_not_terminated(self):
        self.task_image = "other-service.exe"
        self.assertIn("non-Python", self.run_guest()["error"])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_restarts_only_listener_and_reports_deployed_hashes(self):
        report = self.run_guest()
        self.assertEqual("ready", report["phase"], report)
        self.kill.assert_called_once_with(["taskkill", "/PID", "11", "/F"])
        self.start.assert_called_once_with(str(self.batch))
        self.assertEqual([22], report["new_pids"])
        self.assertEqual(self.expected, report["file_hashes"])
        self.assertEqual(["preparing", "starting", "ready"], [report["phase"] for report in self.reports])


if __name__ == "__main__":
    unittest.main()
