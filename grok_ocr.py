import http.client
import json
from base64 import b64encode
import ssl

# 配置常量
API_KEY = "xai-50ZCwcrsIWAcRARytjVMQopQ99vxiEj94iMmmTenWgcg6JlF9ZREKb0ZSEvWK9KpbfqaFhqCmSsdQP2C"  # 替换为你的实际API密钥
API_URL = "api.x.ai"  # API服务器地址


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
    # 读取图片文件并编码为base64
    with open(image_path, "rb") as image_file:
        image_data = b64encode(image_file.read()).decode("utf-8")

    # 构建请求体
    payload = {
        "model": "grok-2-vision-1212",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "What's in this image? Response the characters only, without spaces.",
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
    conn = http.client.HTTPSConnection(API_URL, context=context)

    # 设置请求头
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}

    # 发送POST请求
    conn.request(
        "POST", "/v1/chat/completions", body=json.dumps(payload), headers=headers
    )

    # 获取响应
    response = conn.getresponse()
    response_data = response.read().decode("utf-8")

    # 检查响应状态
    if response.status != 200:
        raise Exception(
            f"API request failed with status {response.status}: {response_data}"
        )

    # 解析响应
    result = json.loads(response_data)

    # 返回识别的文字内容
    return result["choices"][0]["message"]["content"]


if __name__ == "__main__":
    # 示例用法
    image_path = "ocr.png"

    try:
        text = ocr(image_path)
        print("识别结果：")
        print(text)
    except Exception as e:
        print(f"OCR失败: {str(e)}")
