import functools
import logging
from flask import Flask, request, jsonify
from thsauto import ThsAuto
import subprocess
import time
import sys
import threading
import traceback

import os

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

auto = ThsAuto()

client_path = None
def run_client():
    if not client_path:
        raise RuntimeError('未提供客户端路径，无法启动同花顺客户端')
    workdir = os.path.dirname(client_path)
    executable_path = os.path.basename(client_path)
    logging.info("准备启动客户端: path=%s workdir=%s", client_path, workdir)
    subprocess.Popen(["cmd", "/c", "start", "", executable_path], cwd=workdir)


def wait_for_client_bind(max_wait_time=30, bind_timeout=2):
    logging.info(
        "开始等待客户端绑定: max_wait_time=%ss bind_timeout=%ss",
        max_wait_time,
        bind_timeout,
    )
    for second in range(max_wait_time):
        auto.bind_client(timeout=bind_timeout, log_failure=False)
        if auto.hwnd_main is not None:
            logging.info(
                "客户端绑定完成: elapsed=%ss hwnd=%s",
                second + 1,
                auto.hwnd_main,
            )
            return True
        time.sleep(1)
    logging.info("客户端绑定超时: max_wait_time=%ss", max_wait_time)
    return False


def restart_client_process(max_wait_time=30):
    logging.info("开始执行客户端重启流程")
    auto.kill_client()
    run_client()
    return wait_for_client_bind(max_wait_time=max_wait_time, bind_timeout=2)


def ensure_client_started(max_wait_time=30):
    logging.info("启动阶段检查客户端是否已运行")
    auto.bind_client(timeout=2, log_failure=False)
    if auto.hwnd_main is not None:
        logging.info("启动阶段已检测到客户端主窗口: hwnd=%s", auto.hwnd_main)
        return True
    if not client_path:
        logging.info("启动阶段未提供客户端路径，跳过自动拉起")
        return False
    logging.info("启动阶段未检测到客户端，准备自动拉起")
    run_client()
    return wait_for_client_bind(max_wait_time=max_wait_time, bind_timeout=2)

lock = threading.Lock()
next_time = 0
interval = 0.5
def interval_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global interval
        global lock
        global next_time
        lock.acquire()
        now = time.time()
        if now < next_time:
            time.sleep(next_time - now)
        start_time = time.time()
        logging.info(
            "HTTP请求开始: path=%s method=%s args=%s",
            request.path,
            request.method,
            dict(request.args),
        )
        try:
            auto.dismiss_popups()
            rt = func(*args, **kwargs)
        except Exception as e:
            logging.exception("HTTP请求异常: path=%s", request.path)
            traceback.print_exc()
            rt = ({'code': 1, 'status': 'failed', 'msg': '{}'.format(e)}, 400)
        finally:
            elapsed = time.time() - start_time
            logging.info("HTTP请求结束: path=%s elapsed=%.2fs", request.path, elapsed)
        next_time = time.time() + interval
        lock.release()
        return rt
    return wrapper

@app.route('/thsauto/balance', methods = ['GET'])
@interval_call
def get_balance():
    auto.active_main_window()
    result = auto.get_balance()
    return jsonify(result), 200

@app.route('/thsauto/position', methods = ['GET'])
@interval_call
def get_position():
    auto.active_main_window()
    result = auto.get_position()
    return jsonify(result), 200

@app.route('/thsauto/orders/active', methods = ['GET'])
@interval_call
def get_active_orders():
    auto.active_main_window()
    result = auto.get_active_orders()
    return jsonify(result), 200

@app.route('/thsauto/orders/filled', methods = ['GET'])
@interval_call
def get_filled_orders():
    auto.active_main_window()
    result = auto.get_filled_orders()
    return jsonify(result), 200

def _optional_price_arg():
    raw = request.args.get('price', None)
    if raw is None or str(raw).strip() == '':
        return None
    return str(raw).strip()


@app.route('/thsauto/sell', methods = ['GET'])
@interval_call
def sell():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = _optional_price_arg()
    result = auto.sell(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/buy', methods = ['GET'])
@interval_call
def buy():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = _optional_price_arg()
    result = auto.buy(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/buy/kc', methods = ['GET'])
@interval_call
def buy_kc():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = _optional_price_arg()
    result = auto.buy_kc(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/sell/kc', methods = ['GET'])
@interval_call
def sell_kc():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = _optional_price_arg()
    result = auto.sell_kc(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/cancel', methods = ['GET'])
@interval_call
def cancel():
    auto.active_main_window()
    entrust_no = request.args['entrust_no']
    result = auto.cancel(entrust_no=entrust_no)
    return jsonify(result), 200

@app.route('/thsauto/client/kill', methods = ['GET'])
@interval_call
def kill_client():
    auto.active_main_window()
    auto.kill_client()
    return jsonify({'code': 0, 'status': 'succeed'}), 200


@app.route('/thsauto/client/restart', methods = ['GET'])
@interval_call
def restart_client():
    auto.active_main_window()
    max_wait_time = 30
    if restart_client_process(max_wait_time=max_wait_time):
        return jsonify({'code': 0, 'status': 'succeed'}), 200
    return jsonify({'code': 1, 'status': 'failed', 'message': f'Client failed to start within {max_wait_time} seconds'}), 200


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


@app.route('/thsauto/ai', methods = ['GET', 'POST'])
@interval_call
def ai_task():
    payload = {}
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        if not payload and request.form:
            payload = dict(request.form)

    def _param(name, default=None):
        if name in payload and payload[name] is not None:
            return payload[name]
        return request.args.get(name, default)

    prompt = _param('prompt') or _param('task') or _param('query')
    max_steps = _param('max_steps', 12)
    api_url = _param('api_url') or _param('base_url')
    api_key = _param('api_key')
    model = _param('model')
    proxy = _param('proxy')
    timeout = _param('timeout')
    temperature = _param('temperature', 0.1)
    max_tokens = _param('max_tokens', 1500)
    system_prompt = _param('system_prompt')
    extra_body = payload.get('extra_body') if isinstance(payload, dict) else None
    include_screenshot = _parse_bool(_param('include_screenshot'), default=False)

    auto.active_main_window()
    result = auto.run_ai_task(
        prompt=prompt,
        max_steps=int(max_steps) if max_steps is not None else 12,
        api_url=api_url,
        api_key=api_key,
        model=model,
        proxy=proxy,
        timeout=float(timeout) if timeout is not None else None,
        temperature=float(temperature) if temperature is not None else 0.1,
        max_tokens=int(max_tokens) if max_tokens is not None else 1500,
        system_prompt=system_prompt,
        extra_body=extra_body,
        include_screenshot=include_screenshot,
    )
    return jsonify(result), 200


@app.route('/thsauto/test', methods = ['GET'])
@interval_call
def test():
    auto.active_main_window()
    auto.test()
    return jsonify({}), 200


if __name__ == '__main__':
    host = '127.0.0.1'
    port = 5000
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    if len(sys.argv) > 3:
        client_path = sys.argv[3]
    ensure_client_started(max_wait_time=30)
    app.run(host=host, port=port)
