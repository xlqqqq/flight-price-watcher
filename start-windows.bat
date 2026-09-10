@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 正在准备免 token 机票页面和本机微信组件...
where py >nul 2>nul
if errorlevel 1 (
  set "FW_PYTHON=python"
) else (
  set "FW_PYTHON=py -3"
)
%FW_PYTHON% -c "import sys,platform; sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,14) and platform.machine().upper() in ('AMD64','X86_64') else 1)"
if errorlevel 1 (
  echo 请安装 Windows x64 Python 3.11 至 3.13，再运行本文件。
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" %FW_PYTHON% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo 无法创建项目虚拟环境，请检查 Python 安装和文件夹权限。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import importlib.metadata as m; assert m.version('wxauto4') == '41.1.7'" >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install --index-url https://pypi.org/simple -r requirements-wechat.txt
  if errorlevel 1 echo 微信组件安装失败；页面仍可查价，微信发送暂不可用。
)
echo 请打开并登录微信，页面将自动在浏览器中打开。
".venv\Scripts\python.exe" watch.py --web
pause
