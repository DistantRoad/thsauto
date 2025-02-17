import functools
from flask import Flask, request, jsonify
from thsauto import ThsAuto
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
    os.chdir(os.path.dirname(client_path))
    executable_path = os.path.basename(client_path)
    os.system('start ' + executable_path)

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
        try:
            rt = func(*args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            rt = ({'code': 1, 'status': 'failed', 'msg': '{}'.format(e)}, 400)
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

@app.route('/thsauto/sell', methods = ['GET'])
@interval_call
def sell():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = request.args.get('price', None)
    if price is not None:
        price = float(price)
    result = auto.sell(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/buy', methods = ['GET'])
@interval_call
def buy():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = request.args.get('price', None)
    if price is not None:
        price = float(price)
    result = auto.buy(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/buy/kc', methods = ['GET'])
@interval_call
def buy_kc():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = request.args.get('price', None)
    if price is not None:
        price = float(price)
    result = auto.buy_kc(stock_no=stock, amount=int(amount), price=price)
    return jsonify(result), 200

@app.route('/thsauto/sell/kc', methods = ['GET'])
@interval_call
def sell_kc():
    auto.active_main_window()
    stock = request.args['stock_no']
    amount = request.args['amount']
    price = request.args.get('price', None)
    if price is not None:
        price = float(price)
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
    auto.kill_client()
    run_client()
    max_wait_time = 30
    for _ in range(max_wait_time):
        auto.bind_client()
        if auto.hwnd_main is not None:
            return jsonify({'code': 0, 'status': 'succeed'}), 200
        time.sleep(1)
    return jsonify({'code': 1, 'status': 'failed', 'message': f'Client failed to start within {max_wait_time} seconds'}), 200


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
    auto.bind_client()
    if auto.hwnd_main is None and client_path is not None:
        restart_client()
    app.run(host=host, port=port)