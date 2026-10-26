# thsauto - 同花顺自动下单工具

thsauto 是一个基于 Python 的同花顺自动下单工具：通过 pywinauto 模拟用户操作 Windows 版同花顺客户端（网上股票交易系统5.0），并用 Flask 对外提供 HTTP 接口，供量化策略或其他交易系统以程序化方式查询和下单。

## 功能特点

- **账户信息查询**：查询资金（资金余额、冻结金额、可用金额、可取金额、股票市值、总资产、持仓盈亏、当日盈亏、当日盈亏比）和持仓
- **交易操作**：支持普通股票和科创板的买入、卖出，成功时返回合同编号
- **订单管理**：查询未成交、已成交订单，按合同编号撤单
- **客户端管理**：服务启动时自动拉起客户端，支持通过接口关闭、重启客户端
- **验证码自动识别**：截取验证码图片（内存中处理，不落盘），调用兼容 OpenAI 接口的大模型识别，失败自动刷新重试，最多 10 次
- **顺序执行**：全局线程锁保证同一时间只执行一个操作，相邻两次操作至少间隔 0.5 秒
- **窗口自愈**：主窗口过小（如宿主机重启后客户端以很小的窗口启动）时自动放大，避免切页失败
- **日志轮转**：日志按大小自动轮转，不会无限增长

## 运行环境

- Windows 操作系统（需要操作 Windows 客户端，可部署在 Windows 虚拟机中）
- Python 3.6+（Windows 7 最高只能安装 Python 3.8，代码需保持与 3.8 兼容）
- 同花顺股票交易客户端，主窗口标题为 `网上股票交易系统5.0`，快捷键保持默认（F1~F8）
- 可访问的兼容 OpenAI Chat Completions 接口的大模型服务（用于验证码识别），可选 SOCKS5 代理

## 安装

1. 克隆或下载本项目到本地
2. 安装依赖库：
   ```
   pip install flask pillow pywinauto
   ```

## 配置

### OCR 验证码识别

接口地址 `API_URL` 和模型 `MODEL_NAME` 写在 `llm_ocr.py` 顶部的常量中，按需修改。

API Key、代理账号密码等敏感信息**不要写进代码**（本仓库公开），按以下优先级读取：

1. 环境变量
2. 本机配置文件，默认路径为 `%USERPROFILE%\thsauto\ocr_config.json`

| 环境变量 | 配置文件字段 | 说明 |
|---|---|---|
| `THSAUTO_OCR_API_KEY` | `api_key` | 大模型接口的 API Key（必填） |
| `THSAUTO_OCR_PROXY` | `proxy` | SOCKS5 代理，格式 `socks5://用户名:密码@主机:端口`，无需认证时写 `socks5://主机:端口`；不填则直连 |
| `THSAUTO_OCR_CONFIG` | — | 指定配置文件路径 |
| `THSAUTO_OCR_TIMEOUT` | — | 单次识别请求超时秒数，默认 `15` |

配置文件示例：

```json
{
  "api_key": "sk-...",
  "proxy": "socks5://用户名:密码@主机:端口"
}
```

说明：

- 环境变量只要设置了（即使值为空）就优先于配置文件，可用 `THSAUTO_OCR_PROXY=` 临时禁用代理
- 每次识别时都会重新读取配置文件，修改后无需重启服务
- 日志中只记录代理的主机和端口，不记录账号密码

### 日志

日志同时输出到控制台和日志文件，日志文件按大小轮转：超过上限后依次改名为 `thsauto.log.1`、`thsauto.log.2`……，超出保留个数的最旧文件会被删除。按默认值计算，日志最多占用约 110MB。

| 环境变量 | 说明 |
|---|---|
| `THSAUTO_LOG_FILE` | 日志文件路径，默认为启动目录下的 `thsauto.log` |
| `THSAUTO_LOG_MAX_BYTES` | 单个日志文件大小上限（字节），默认 `10485760`（10MB）；设为 `0` 表示不轮转 |
| `THSAUTO_LOG_BACKUP_COUNT` | 保留的旧日志文件个数，默认 `10`，最小为 `1` |

注意：同一个日志文件只能由一个服务进程写入。在 Windows 上文件被其他进程占用时无法改名，轮转会失败；同一台机器运行多个实例时，请分别设置不同的 `THSAUTO_LOG_FILE`。

## 使用方法

### 启动服务

```bash
python server.py [IP地址] [端口] [同花顺客户端路径]
```

- IP 地址默认 `127.0.0.1`，端口默认 `5000`；需要被其他机器访问时，IP 写 `0.0.0.0` 或本机局域网地址
- 提供客户端路径时，如果启动时客户端未运行，会自动拉起并等待最多 30 秒完成绑定；`/thsauto/client/restart` 接口也依赖该路径

示例：

```bash
python server.py 0.0.0.0 5000 C:\Users\match\Desktop\THS\xiadan.exe
```

### 自动更新部署

适用于长期运行的 Windows 主机或虚拟机：每次启动时自动从 GitHub 拉取 master 分支的最新代码。涉及两个文件：

- `start_ths_server.bat`：桌面引导脚本（GBK 编码、CRLF 换行），复制到桌面并设为开机自启。它只负责把 master 分支压缩包下载解压到 `%USERPROFILE%\thsauto\download`，然后调用其中的 `start_server.py`；下载失败时改为调用上次的 `code\start_server.py`。这个文件修改后需要逐台替换，尽量保持不变
- `start_server.py`：启动逻辑。确认新代码完整后替换 `%USERPROFILE%\thsauto\code`（旧代码保留在 `code_old`），再以 `%USERPROFILE%\thsauto` 为工作目录启动 `server.py`。修改后推送到 GitHub，下次启动时生效

运行目录结构：

```
%USERPROFILE%\thsauto\
├── code\             当前运行的代码
├── code_old\         上一版代码
├── download\         本次启动下载的代码
├── ocr_config.json   OCR 配置（见上文）
└── thsauto.log       日志
```

`start_server.py` 的运行参数可以在桌面脚本中用环境变量覆盖：

| 环境变量 | 说明 |
|---|---|
| `THSAUTO_HOME` | 运行目录，默认 `%USERPROFILE%\thsauto` |
| `THSAUTO_HOST` | 监听地址，默认 `0.0.0.0` |
| `THSAUTO_PORT` | 监听端口，默认 `6003` |
| `THS_CLIENT_PATH` | 同花顺客户端路径，默认 `%USERPROFILE%\Desktop\同花顺\xiadan.exe` |
| `THSAUTO_LOG_FILE` | 日志文件，默认 `%USERPROFILE%\thsauto\thsauto.log` |

更新代码时，推送到 master 后结束机器上的 `py.exe` / `python.exe` 进程，再重新运行桌面脚本即可。注意：推送到 master 的代码会在每台机器下次启动（包括意外重启）时自动生效，推送前请确认代码可以正常运行。

### 返回格式

所有接口均为 HTTP GET，返回 JSON：

```json
{"code": 0, "status": "succeed", "data": ...}
{"code": 1, "status": "failed", "msg": "失败原因"}
```

- `code` 为 `0` 表示成功，非 `0` 表示失败
- 查询类接口的结果放在 `data` 中，表格类数据为列表，每行是一个以表头为键的对象
- 下单成功时额外返回 `msg`（客户端提示文本）和 `entrust_no`（合同编号）
- 请求处理过程中抛出异常时返回 HTTP 400，`msg` 为异常信息

### 接口列表

以下地址均以 `http://<IP>:<端口>` 开头。

#### 账户信息查询

| 接口 | 说明 |
|---|---|
| `/thsauto/balance` | 查询资金 |
| `/thsauto/position` | 查询持仓 |

#### 交易操作

| 接口 | 说明 |
|---|---|
| `/thsauto/buy?stock_no=<股票代码>&price=<价格>&amount=<数量>` | 买入 |
| `/thsauto/sell?stock_no=<股票代码>&price=<价格>&amount=<数量>` | 卖出 |
| `/thsauto/buy/kc?stock_no=<股票代码>&price=<价格>&amount=<数量>` | 科创板买入 |
| `/thsauto/sell/kc?stock_no=<股票代码>&price=<价格>&amount=<数量>` | 科创板卖出 |

- `amount` 为整数股数
- `price` 可省略，省略时不填写价格，使用客户端根据证券代码自动带出的价格

#### 订单管理

| 接口 | 说明 |
|---|---|
| `/thsauto/orders/active` | 查询未成交订单 |
| `/thsauto/orders/filled` | 查询已成交订单 |
| `/thsauto/cancel?entrust_no=<合同编号>` | 撤单，`entrust_no` 为下单时返回的合同编号 |

#### 客户端管理

| 接口 | 说明 |
|---|---|
| `/thsauto/client/kill` | 关闭同花顺客户端 |
| `/thsauto/client/restart` | 重启同花顺客户端，最多等待 30 秒完成绑定 |

## 技术实现

- **界面自动化**：使用 pywinauto（win32 后端）绑定窗口、定位控件、输入、点击和截图，通过客户端快捷键切换买入、卖出、撤单、查询等页面
- **表格读取**：对客户端表格控件执行复制，从剪贴板读取文本后解析为结构化数据
- **验证码识别**：`llm_ocr.py` 将截图以 base64 发送到兼容 OpenAI 的 Chat Completions 接口，内置 SOCKS5 代理支持，不依赖第三方 HTTP 库
- **Web API**：使用 Flask 提供 HTTP 接口

## 部署注意事项

- 自动化依赖真实的桌面会话：截图和鼠标点击都要求桌面处于已登录、未锁屏状态。用远程桌面连接后直接断开，可能导致验证码截图空白、点击失效，建议通过虚拟机控制台查看
- 服务运行期间不要手动操作同花顺客户端，以免打乱窗口焦点和页面状态
- 客户端异常时可调用 `/thsauto/client/restart` 重启

## 测试

- `test_llm_ocr.py`：OCR 模块单元测试，不依赖客户端，运行 `python -m unittest test_llm_ocr`
- `test.py`：手动调试脚本，需要客户端已运行

## 注意事项

- 本工具仅用于个人学习和研究，不得用于商业用途
- 使用本工具进行交易操作时，请确保了解相关风险
- 本工具不对任何交易损失负责

## 贡献指南

欢迎提交问题和功能需求，也欢迎提交Pull Request贡献代码。

## 许可证

MIT License
