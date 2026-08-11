import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import time
from base64 import b64encode
from urllib.parse import unquote, urlparse


# 配置常量
API_KEY = "sk-AqjcrzF1uR3G9NBICeB1A1Af71984f4eA6D4182bE167B60d"  # 替换为你的实际API密钥
API_URL = "new-api.distantroad.win"  # API服务器地址
# MODEL_NAME = "gemini-2.5-flash"
MODEL_NAME = "gemini-3.1-flash-lite"
PROMPT_TEXT = (
    "Read the captcha characters exactly as shown. Preserve uppercase/lowercase exactly."
    " Return the characters only, with no spaces or extra text."
)
REQUEST_TIMEOUT = float(os.getenv("THSAUTO_OCR_TIMEOUT", "15"))
OCR_PROXY_URL = os.getenv(
    "THSAUTO_OCR_PROXY",
    "socks5://wang:wdy988299@192.168.2.88:10888",
).strip()


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("SOCKS5代理连接提前关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_proxy_url(proxy_url: str):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() != "socks5":
        raise ValueError(f"仅支持 socks5 代理，当前为: {parsed.scheme}")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"代理地址格式无效: {proxy_url}")
    return {
        "scheme": parsed.scheme.lower(),
        "host": parsed.hostname,
        "port": parsed.port,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }


def _build_socks5_address(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            return bytes([0x01]) + ip.packed
        return bytes([0x04]) + ip.packed
    except ValueError:
        host_bytes = host.encode("idna")
        if len(host_bytes) > 255:
            raise ValueError(f"目标主机名过长: {host}")
        return bytes([0x03, len(host_bytes)]) + host_bytes


def _create_socks5_connection(host: str, port: int, timeout: float, proxy_url: str):
    proxy = _parse_proxy_url(proxy_url)
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)

    logging.info(
        "通过SOCKS5代理建立连接: proxy=%s://%s:%s target=%s:%s timeout=%.1fs",
        proxy["scheme"],
        proxy["host"],
        proxy["port"],
        host,
        port,
        timeout,
    )
    sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
    sock.settimeout(timeout)
    try:
        methods = [0x00]
        if proxy["username"] is not None:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods), *methods]))
        version, method = _recv_exact(sock, 2)
        if version != 0x05:
            raise ConnectionError(f"SOCKS5握手失败，版本错误: {version}")
        if method == 0xFF:
            raise ConnectionError("SOCKS5代理不接受当前认证方式")

        if method == 0x02:
            username = (proxy["username"] or "").encode("utf-8")
            password = (proxy["password"] or "").encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise ValueError("SOCKS5用户名或密码长度不能超过255字节")
            auth_payload = (
                bytes([0x01, len(username)])
                + username
                + bytes([len(password)])
                + password
            )
            sock.sendall(auth_payload)
            auth_version, auth_status = _recv_exact(sock, 2)
            if auth_version != 0x01 or auth_status != 0x00:
                raise ConnectionError(
                    f"SOCKS5认证失败: version={auth_version} status={auth_status}"
                )

        address = _build_socks5_address(host)
        request = bytes([0x05, 0x01, 0x00]) + address + port.to_bytes(2, "big")
        sock.sendall(request)

        header = _recv_exact(sock, 4)
        version, reply, _, atyp = header
        if version != 0x05:
            raise ConnectionError(f"SOCKS5响应版本错误: {version}")
        if reply != 0x00:
            raise ConnectionError(f"SOCKS5连接失败: reply={reply}")

        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x03:
            domain_length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, domain_length)
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        else:
            raise ConnectionError(f"SOCKS5响应地址类型不支持: {atyp}")
        _recv_exact(sock, 2)
        logging.info("SOCKS5代理连接建立成功: target=%s:%s", host, port)
        return sock
    except Exception:
        sock.close()
        raise


class SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, proxy_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = proxy_url

    def connect(self):
        raw_sock = _create_socks5_connection(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            proxy_url=self.proxy_url,
        )
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


def ocr(image_path: str) -> str:
    """
    使用OpenAI兼容的OCR接口识别图片中的文字

    Args:
        image_path: 图片文件路径
        api_key: OpenAI API密钥
        api_url: API服务器地址，默认为api.openai.com

    Returns:
        识别出的文字内容
    """
    logging.info(
        "OCR请求开始: image_path=%s model=%s timeout=%.1fs proxy=%s",
        image_path,
        MODEL_NAME,
        REQUEST_TIMEOUT,
        OCR_PROXY_URL or "disabled",
    )
    start_time = time.time()

    # 读取图片文件并编码为base64
    with open(image_path, "rb") as image_file:
        image_data = b64encode(image_file.read()).decode("utf-8")

    # 构建请求体
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": PROMPT_TEXT,
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                    },
                ],
            }
        ],
        "max_tokens": 300,
    }

    # 创建HTTP连接，禁用SSL验证
    context = ssl._create_unverified_context()
    conn = SocksHTTPSConnection(
        API_URL,
        timeout=REQUEST_TIMEOUT,
        context=context,
        proxy_url=OCR_PROXY_URL or None,
    )

    # 设置请求头
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}

    # 发送POST请求
    try:
        conn.request(
            "POST", "/v1/chat/completions", body=json.dumps(payload), headers=headers
        )

        # 获取响应
        response = conn.getresponse()
        response_data = response.read().decode("utf-8")
        elapsed = time.time() - start_time
        logging.info(
            "OCR请求完成: status=%s elapsed=%.2fs response_bytes=%d",
            response.status,
            elapsed,
            len(response_data),
        )

        # 检查响应状态
        if response.status != 200:
            raise Exception(
                f"API request failed with status {response.status}: {response_data}"
            )

        # 解析响应
        result = json.loads(response_data)
        content = result["choices"][0]["message"]["content"]
        logging.info("OCR识别原始结果: %r", content)
        return content
    except Exception:
        logging.exception(
            "OCR请求失败: image_path=%s api_url=%s timeout=%.1fs",
            image_path,
            API_URL,
            REQUEST_TIMEOUT,
        )
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    # 示例用法
    image_path = "ocr.png"

    try:
        text = ocr(image_path)
        print("识别结果：")
        print(text)
    except Exception as e:
        print(f"OCR失败: {str(e)}")
