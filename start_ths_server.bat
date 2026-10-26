@echo off
rem thsauto 桌面引导脚本：放在各虚拟机桌面上（开机自启），只负责从 GitHub 下载最新代码。
rem 替换代码目录、启动服务等逻辑都在仓库的 start_server.py 里，修改后推送到 GitHub 即可生效。
rem 本文件为 GBK 编码、CRLF 换行，修改后需逐台替换，请尽量保持不变。
setlocal
title thsauto server
set THSAUTO_HOME=%USERPROFILE%\thsauto
set THSAUTO_ZIP_URL=https://codeload.github.com/DistantRoad/thsauto/zip/refs/heads/master
rem 客户端不在默认位置（%USERPROFILE%\Desktop\同花顺\xiadan.exe）的虚拟机，取消下一行注释并修改路径
rem set THS_CLIENT_PATH=%USERPROFILE%\Desktop\同花顺\xiadan.exe

if not exist "%THSAUTO_HOME%" mkdir "%THSAUTO_HOME%"
cd /d "%THSAUTO_HOME%"
if exist download rmdir /s /q download

echo 正在从 GitHub 下载 thsauto 最新代码...
rem Win7 系统根证书不全，额外加载 pip 自带的证书包来校验 GitHub 证书
py -c "import io,ssl,sys,zipfile,urllib.request as r; from pip._vendor import certifi; c=ssl.create_default_context(); c.load_verify_locations(certifi.where()); zipfile.ZipFile(io.BytesIO(r.urlopen(sys.argv[1],timeout=60,context=c).read())).extractall(sys.argv[2])" "%THSAUTO_ZIP_URL%" download
if errorlevel 1 (
    echo 下载最新代码失败，继续使用上次下载的代码
    if exist download rmdir /s /q download
)

if exist download\thsauto-master\start_server.py (
    py download\thsauto-master\start_server.py
) else if exist code\start_server.py (
    py code\start_server.py
) else (
    echo 本地没有可用的 thsauto 代码，无法启动服务
    pause
)
