import http.client
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import ssl
import time
from base64 import b64encode
from io import BytesIO
from urllib.parse import unquote, urlparse


# 配置常量
API_URL = "new-api.distantroad.win"  # API服务器地址
MODEL_NAME = "gemini-3.5-flash-lite"
PROMPT_TEXT = (
    "Read the captcha characters exactly as shown. Preserve uppercase/lowercase exactly."
    " Return the characters only, with no spaces or extra text."
)
REQUEST_TIMEOUT = float(os.getenv("THSAUTO_OCR_TIMEOUT", "15"))
AI_REQUEST_TIMEOUT = float(os.getenv("THSAUTO_AI_TIMEOUT", "60"))
# API Key、代理账号密码等敏感配置不能写在代码里（本仓库公开）。
# 优先读环境变量，其次读本机配置文件；配置文件默认为 %USERPROFILE%\thsauto\ocr_config.json，
# 可用环境变量 THSAUTO_OCR_CONFIG 指定路径，内容示例：
# {"api_key": "sk-...", "proxy": "socks5://用户名:密码@主机:端口"}
OCR_CONFIG_PATH = os.path.abspath(
    os.path.expanduser(
        os.getenv("THSAUTO_OCR_CONFIG", os.path.join("~", "thsauto", "ocr_config.json"))
    )
)


def _load_local_config() -> dict:
    try:
        with open(OCR_CONFIG_PATH, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        logging.warning("未找到OCR本机配置文件: %s", OCR_CONFIG_PATH)
        return {}
    except Exception:
        logging.exception("读取OCR本机配置文件失败: %s", OCR_CONFIG_PATH)
        return {}
    if not isinstance(config, dict):
        logging.warning("OCR本机配置文件格式无效（应为JSON对象）: %s", OCR_CONFIG_PATH)
        return {}
    return config


def _get_setting(env_name: str, config_key: str, config: dict) -> str:
    # 环境变量只要设置了（即使为空）就优先使用，便于临时禁用代理
    value = os.getenv(env_name)
    if value is None:
        value = config.get(config_key) or ""
    return str(value).strip()


def _get_ocr_settings():
    """每次请求时读取，修改配置文件后无需重启服务。"""
    config = _load_local_config()
    api_key = _get_setting("THSAUTO_OCR_API_KEY", "api_key", config)
    proxy_url = _get_setting("THSAUTO_OCR_PROXY", "proxy", config)
    if not api_key:
        raise RuntimeError(
            "未配置OCR API Key：请设置环境变量 THSAUTO_OCR_API_KEY，"
            f"或在 {OCR_CONFIG_PATH} 中配置 api_key"
        )
    return api_key, proxy_url


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


def _describe_proxy(proxy_url: str) -> str:
    """日志里只显示代理主机和端口，隐藏账号密码。"""
    if not proxy_url:
        return "disabled"
    parsed = urlparse(proxy_url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


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


class SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, proxy_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_url = proxy_url

    def connect(self):
        self.sock = _create_socks5_connection(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            proxy_url=self.proxy_url,
        )


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


def _parse_api_endpoint(api_url: str):
    raw = (api_url or API_URL).strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    host = parsed.hostname or API_URL
    port = parsed.port or (80 if scheme == "http" else 443)
    path = (parsed.path or "").rstrip("/")
    if not path:
        request_path = "/v1/chat/completions"
    elif path.endswith("/chat/completions"):
        request_path = path
    elif path.endswith("/v1"):
        request_path = f"{path}/chat/completions"
    else:
        request_path = f"{path}/v1/chat/completions"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    return scheme, host, port, request_path


def resolve_llm_settings(
    api_url=None,
    api_key=None,
    model=None,
    proxy=None,
    timeout=None,
    default_timeout=None,
):
    """合并请求级参数、环境变量与本机配置文件，支持每次请求手动覆盖。"""
    config = _load_local_config()
    resolved_api_url = (
        str(api_url).strip()
        if api_url is not None and str(api_url).strip()
        else (
            _get_setting("THSAUTO_AI_API_URL", "api_url", config)
            or _get_setting("THSAUTO_OCR_API_URL", "api_url", config)
            or API_URL
        )
    )
    resolved_api_key = (
        str(api_key).strip()
        if api_key is not None and str(api_key).strip()
        else (
            _get_setting("THSAUTO_AI_API_KEY", "ai_api_key", config)
            or _get_setting("THSAUTO_OCR_API_KEY", "api_key", config)
        )
    )
    if not resolved_api_key:
        raise RuntimeError(
            "未配置API Key：请在请求参数中传入 api_key，设置环境变量 THSAUTO_OCR_API_KEY，"
            f"或在 {OCR_CONFIG_PATH} 中配置 api_key"
        )

    resolved_model = (
        str(model).strip()
        if model is not None and str(model).strip()
        else (
            _get_setting("THSAUTO_AI_MODEL", "ai_model", config)
            or _get_setting("THSAUTO_OCR_MODEL", "model", config)
            or MODEL_NAME
        )
    )

    if proxy is not None:
        resolved_proxy = str(proxy).strip()
    elif os.getenv("THSAUTO_AI_PROXY") is not None:
        resolved_proxy = os.getenv("THSAUTO_AI_PROXY", "").strip()
    else:
        resolved_proxy = _get_setting("THSAUTO_OCR_PROXY", "proxy", config)

    fallback_timeout = AI_REQUEST_TIMEOUT if default_timeout is None else float(default_timeout)
    resolved_timeout = float(timeout) if timeout is not None else fallback_timeout

    return {
        "api_url": resolved_api_url,
        "api_key": resolved_api_key,
        "model": resolved_model,
        "proxy": resolved_proxy,
        "timeout": resolved_timeout,
    }


def _post_chat_completions(
    api_url: str,
    api_key: str,
    proxy_url: str,
    timeout: float,
    payload: dict,
    log_label: str = "LLM",
) -> str:
    scheme, host, port, request_path = _parse_api_endpoint(api_url)
    start_time = time.time()
    if scheme == "http":
        conn = SocksHTTPConnection(
            host,
            port=port,
            timeout=timeout,
            proxy_url=proxy_url or None,
        )
    else:
        context = ssl._create_unverified_context()
        conn = SocksHTTPSConnection(
            host,
            port=port,
            timeout=timeout,
            context=context,
            proxy_url=proxy_url or None,
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        conn.request("POST", request_path, body=json.dumps(payload), headers=headers)
        response = conn.getresponse()
        response_data = response.read().decode("utf-8")
        elapsed = time.time() - start_time
        logging.info(
            "%s请求完成: status=%s elapsed=%.2fs response_bytes=%d",
            log_label,
            response.status,
            elapsed,
            len(response_data),
        )
        if response.status != 200:
            raise RuntimeError(
                f"API request failed with status {response.status}: {response_data}"
            )
        result = json.loads(response_data)
        return result["choices"][0]["message"]["content"]
    finally:
        conn.close()


def request_chat_completion(
    messages: list,
    api_url=None,
    api_key=None,
    model=None,
    proxy=None,
    timeout=None,
    temperature=None,
    max_tokens=1500,
    extra_body=None,
) -> str:
    """通用 OpenAI 兼容 Chat Completions 调用入口，支持多模态与手动指定配置。"""
    settings = resolve_llm_settings(
        api_url=api_url,
        api_key=api_key,
        model=model,
        proxy=proxy,
        timeout=timeout,
    )
    scheme, host, port, request_path = _parse_api_endpoint(settings["api_url"])
    logging.info(
        "LLM请求开始: endpoint=%s://%s:%s%s model=%s timeout=%.1fs proxy=%s",
        scheme,
        host,
        port,
        request_path,
        settings["model"],
        settings["timeout"],
        _describe_proxy(settings["proxy"]),
    )

    payload = {
        "model": settings["model"],
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if isinstance(extra_body, dict):
        payload.update(extra_body)

    try:
        return _post_chat_completions(
            api_url=settings["api_url"],
            api_key=settings["api_key"],
            proxy_url=settings["proxy"],
            timeout=settings["timeout"],
            payload=payload,
            log_label="LLM",
        )
    except Exception:
        logging.exception(
            "LLM请求失败: api_url=%s model=%s timeout=%.1fs",
            settings["api_url"],
            settings["model"],
            settings["timeout"],
        )
        raise


def _request_ocr(
    image_bytes: bytes,
    media_type: str,
    image_source: str,
    api_url=None,
    api_key=None,
    model=None,
    proxy=None,
    timeout=None,
) -> str:
    has_override = any(v is not None for v in (api_url, api_key, model, proxy, timeout))
    if not has_override:
        resolved_key, resolved_proxy = _get_ocr_settings()
        resolved_url = API_URL
        resolved_model = MODEL_NAME
        resolved_timeout = REQUEST_TIMEOUT
    else:
        settings = resolve_llm_settings(
            api_url=api_url,
            api_key=api_key,
            model=model,
            proxy=proxy,
            timeout=timeout,
            default_timeout=REQUEST_TIMEOUT,
        )
        resolved_key = settings["api_key"]
        resolved_proxy = settings["proxy"]
        resolved_url = settings["api_url"]
        resolved_model = settings["model"]
        resolved_timeout = settings["timeout"]

    logging.info(
        "OCR请求开始: image_source=%s image_bytes=%d model=%s timeout=%.1fs proxy=%s",
        image_source,
        len(image_bytes),
        resolved_model,
        resolved_timeout,
        _describe_proxy(resolved_proxy),
    )
    image_data = b64encode(image_bytes).decode("utf-8")

    payload = {
        "model": resolved_model,
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
                        "image_url": {"url": f"data:{media_type};base64,{image_data}"},
                    },
                ],
            }
        ],
        "max_tokens": 300,
    }

    try:
        content = _post_chat_completions(
            api_url=resolved_url,
            api_key=resolved_key,
            proxy_url=resolved_proxy,
            timeout=resolved_timeout,
            payload=payload,
            log_label="OCR",
        )
        logging.info("OCR识别原始结果: %r", content)
        return content
    except Exception:
        logging.exception(
            "OCR请求失败: image_source=%s api_url=%s timeout=%.1fs",
            image_source,
            resolved_url,
            resolved_timeout,
        )
        raise


def ocr_image(image, **llm_kwargs) -> str:
    """直接识别Pillow图片对象，不在磁盘创建临时文件。"""
    with BytesIO() as image_buffer:
        image.save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()
    return _request_ocr(image_bytes, "image/png", "memory", **llm_kwargs)


def ocr(image_path: str) -> str:
    """识别图片文件；保留该入口以兼容已有调用方。"""
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()
    media_type = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
    return _request_ocr(image_bytes, media_type, os.fspath(image_path))


if __name__ == "__main__":
    # 示例用法
    image_path = "ocr.png"

    try:
        text = ocr(image_path)
        print("识别结果：")
        print(text)
    except Exception as e:
        print(f"OCR失败: {str(e)}")
