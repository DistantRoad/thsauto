import json
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

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


if __name__ == "__main__":
    unittest.main()
