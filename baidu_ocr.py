import base64
import urllib.request
import urllib.parse
import json
import ssl

API_KEY = "cVuXPpOguPhOiHrLBwfx5Gaz"
SECRET_KEY = "C6D2zfdv5kEwQziLbjBYqEOH7WMoYdhB"

# Create an unverified SSL context
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

def image_to_base64(image_path):
    """
    Convert an image file to base64 encoding and URL encode the result.
    
    :param image_path: The path to the image file
    :return: URL encoded base64 string of the image
    """
    try:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return urllib.parse.quote(encoded_string)
    except IOError:
        print(f"Error: Unable to read the file at {image_path}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")
        return None

def ocr(image_path):
    url = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token=" + get_access_token()
    image_base64 = image_to_base64(image_path)
    payload = f'image={image_base64}&detect_direction=false&detect_language=false&paragraph=false&probability=false'
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json'
    }
    
    req = urllib.request.Request(url, data=payload.encode('utf-8'), headers=headers, method='POST')
    with urllib.request.urlopen(req, context=ssl_context) as response:
        result = json.loads(response.read().decode('utf-8'))
    if 'words_result' in result and len(result['words_result']) > 0:
        return result['words_result'][0]['words']
    else:
        return ""

def get_access_token():
    """
    使用 AK，SK 生成鉴权签名（Access Token）
    :return: access_token，或是None(如果错误)
    """
    url = "https://aip.baidubce.com/oauth/2.0/token"
    params = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": API_KEY, "client_secret": SECRET_KEY})
    req = urllib.request.Request(url + "?" + params)
    with urllib.request.urlopen(req, context=ssl_context) as response:
        result = json.loads(response.read().decode('utf-8'))
    return str(result.get("access_token"))

if __name__ == '__main__':
    print(ocr("ocr.png"))