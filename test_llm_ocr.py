import json
import os
import tempfile
import unittest
from unittest import mock

try:
    from PIL import Image
except ImportError:
    class _DummyImage:
        def save(self, buffer, format="PNG"):
            buffer.write(b"\x89PNG\r\n\x1a\n")

    class Image:
        @staticmethod
        def new(mode, size, color=None):
            return _DummyImage()

import llm_ocr


class _FakeResponse:
    status = 200

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": "A1b2"}}]}
        ).encode("utf-8")


class _FakeConnection:
    def __init__(self):
        self.request_body = None
        self.closed = False

    def request(self, method, path, body, headers):
        self.request_body = json.loads(body)

    def getresponse(self):
        return _FakeResponse()

    def close(self):
        self.closed = True


class OcrImageTests(unittest.TestCase):
    def test_ocr_image_sends_png_from_memory_without_creating_files(self):
        image = Image.new("RGB", (12, 6), "white")
        connection = _FakeConnection()

        with tempfile.TemporaryDirectory() as working_directory:
            previous_working_directory = os.getcwd()
            os.chdir(working_directory)
            try:
                with mock.patch.object(
                    llm_ocr,
                    "SocksHTTPSConnection",
                    return_value=connection,
                ), mock.patch.dict(
                    os.environ,
                    {"THSAUTO_OCR_API_KEY": "test-key", "THSAUTO_OCR_PROXY": ""},
                ):
                    result = llm_ocr.ocr_image(image)

                self.assertEqual("A1b2", result)
                self.assertEqual([], os.listdir(working_directory))
            finally:
                os.chdir(previous_working_directory)

        image_url = connection.request_body["messages"][0]["content"][1][
            "image_url"
        ]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertTrue(connection.closed)


class OcrSettingsTests(unittest.TestCase):
    def test_settings_are_read_from_local_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "ocr_config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"api_key": " file-key ", "proxy": "socks5://u:p@127.0.0.1:1080"}, config_file)
            with mock.patch.object(llm_ocr, "OCR_CONFIG_PATH", config_path), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                self.assertEqual(
                    ("file-key", "socks5://u:p@127.0.0.1:1080"), llm_ocr._get_ocr_settings()
                )

    def test_environment_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "ocr_config.json")
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump({"api_key": "file-key", "proxy": "socks5://u:p@127.0.0.1:1080"}, config_file)
            with mock.patch.object(llm_ocr, "OCR_CONFIG_PATH", config_path), mock.patch.dict(
                os.environ, {"THSAUTO_OCR_API_KEY": "env-key", "THSAUTO_OCR_PROXY": ""}, clear=True
            ):
                self.assertEqual(("env-key", ""), llm_ocr._get_ocr_settings())

    def test_missing_api_key_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "missing.json")
            with mock.patch.object(llm_ocr, "OCR_CONFIG_PATH", config_path), mock.patch.dict(
                os.environ, {}, clear=True
            ):
                with self.assertRaisesRegex(RuntimeError, "未配置OCR API Key"):
                    llm_ocr._get_ocr_settings()

    def test_proxy_description_hides_credentials(self):
        self.assertEqual(
            "socks5://192.168.2.88:10888",
            llm_ocr._describe_proxy("socks5://user:secret@192.168.2.88:10888"),
        )

    def test_request_chat_completion_allows_manual_overrides(self):
        connection = _FakeConnection()
        with mock.patch.object(
            llm_ocr,
            "SocksHTTPSConnection",
            return_value=connection,
        ):
            reply = llm_ocr.request_chat_completion(
                messages=[{"role": "user", "content": "ping"}],
                api_url="https://api.example.com/v1",
                api_key="manual-key",
                model="custom-vision-model",
                proxy="",
                temperature=0.2,
                max_tokens=800,
                extra_body={"top_p": 0.9},
            )
        self.assertEqual("A1b2", reply)
        self.assertEqual("custom-vision-model", connection.request_body["model"])
        self.assertEqual(0.2, connection.request_body["temperature"])
        self.assertEqual(800, connection.request_body["max_tokens"])
        self.assertEqual(0.9, connection.request_body["top_p"])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
