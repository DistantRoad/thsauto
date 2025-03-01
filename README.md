# thsauto - 同花顺自动下单工具

thsauto是一个基于Python开发的同花顺自动下单工具，通过模拟用户操作同花顺客户端，实现股票交易的自动化。项目提供了一系列HTTP API接口，使用户可以通过程序化方式进行股票交易相关操作。

## 功能特点

- **账户信息查询**：查询资金账户余额、持仓情况
- **交易操作**：支持普通股票和科创板的买入、卖出操作
- **订单管理**：查询未成交订单、已成交订单，以及撤单操作
- **客户端管理**：支持关闭和重启同花顺客户端
- **多种OCR支持**：支持多种OCR服务，提高验证码识别的成功率
- **线程安全**：使用线程锁机制确保操作的顺序执行，避免并发问题

## 安装要求

- Python 3.6+
- Windows操作系统（因为需要操作Windows客户端）
- 同花顺股票交易客户端（网上股票交易系统5.0）

## 安装和配置

1. 克隆或下载本项目到本地
2. 安装依赖库：
   ```
   pip install flask pillow pywin32
   ```
3. 如需使用OCR功能，请配置相应的API密钥：
   - 对于百度OCR，在`baidu_ocr.py`中配置API_KEY和SECRET_KEY
   - 对于Grok OCR，在`grok_ocr.py`中配置API_KEY

## 使用方法

### 启动服务器

```bash
python server.py <IP地址> <端口> <同花顺客户端路径>
```

示例：
```bash
python server.py 192.168.0.116 5000 C:\Users\match\Desktop\THS\xiadan.exe
```

### API接口说明

所有API接口均以HTTP GET方式提供，返回JSON格式数据。

#### 账户信息查询

- **查询资金账户**  
  `http://<IP>:<端口>/thsauto/balance`  
  
- **查询持仓**  
  `http://<IP>:<端口>/thsauto/position`  

#### 交易操作

- **买入下单**  
  `http://<IP>:<端口>/thsauto/buy?stock_no=<股票代码>&price=<价格>&amount=<数量>`  
  
- **卖出下单**  
  `http://<IP>:<端口>/thsauto/sell?stock_no=<股票代码>&price=<价格>&amount=<数量>`  
  
- **科创板买入下单**  
  `http://<IP>:<端口>/thsauto/buy/kc?stock_no=<股票代码>&price=<价格>&amount=<数量>`  
  
- **科创板卖出下单**  
  `http://<IP>:<端口>/thsauto/sell/kc?stock_no=<股票代码>&price=<价格>&amount=<数量>`  

#### 订单管理

- **查询未成交订单**  
  `http://<IP>:<端口>/thsauto/orders/active`  
  
- **查询已成交订单**  
  `http://<IP>:<端口>/thsauto/orders/filled`  
  
- **撤单**  
  `http://<IP>:<端口>/thsauto/cancel?entrust_no=<委托编号>`  

#### 客户端管理

- **关闭同花顺客户端**  
  `http://<IP>:<端口>/thsauto/client/kill`  
  
- **重启同花顺客户端**  
  `http://<IP>:<端口>/thsauto/client/restart`  

## 技术实现

- **界面自动化**：使用win32api、win32gui等Windows API实现界面操作
- **OCR识别**：支持Grok AI和百度OCR服务，用于识别验证码等图像内容
- **Web API**：使用Flask框架提供RESTful API

## 应用场景

- **量化交易**：可以与量化交易策略结合，实现自动化交易
- **交易系统集成**：可以集成到其他交易系统中，作为执行层
- **自动化测试**：可以用于同花顺客户端的自动化测试

## 注意事项

- 本工具仅用于个人学习和研究，不得用于商业用途
- 使用本工具进行交易操作时，请确保了解相关风险
- 本工具不对任何交易损失负责

## 贡献指南

欢迎提交问题和功能需求，也欢迎提交Pull Request贡献代码。

## 许可证

MIT License
