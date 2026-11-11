@echo off
REM legal-demo API 服务管理（WinSW 包装 uvicorn）
REM 用法：
REM   service_ctl.bat install    安装并设置开机自启
REM   service_ctl.bat start      启动
REM   service_ctl.bat stop       停止
REM   service_ctl.bat restart    重启
REM   service_ctl.bat status     查看状态
REM   service_ctl.bat uninstall  卸载
setlocal
set SVC_DIR=%~dp0
set EXE=%SVC_DIR%LegalDemoService.exe

if not exist "%EXE%" (
  echo [错误] 未找到 %EXE%，请先下载 WinSW-x64.exe 并重命名为 LegalDemoService.exe
  exit /b 1
)

if "%~1"=="" goto usage
if /i "%~1"=="install" (
  "%EXE%" install
  if errorlevel 1 exit /b 1
  sc config LegalDemoAPI start= auto
  sc failure LegalDemoAPI reset= 3600 actions= restart/10000/restart/30000/restart/60000
  echo [完成] 服务已安装并设为开机自启
  goto end
)
if /i "%~1"=="start" (
  "%EXE%" start & goto end
)
if /i "%~1"=="stop" (
  "%EXE%" stop & goto end
)
if /i "%~1"=="restart" (
  "%EXE%" restart & goto end
)
if /i "%~1"=="status" (
  sc query LegalDemoAPI
  goto end
)
if /i "%~1"=="uninstall" (
  "%EXE%" stop >nul 2>&1
  "%EXE%" uninstall & goto end
)

:usage
echo 用法: service_ctl.bat [install^|start^|stop^|restart^|status^|uninstall]
exit /b 1

:end
endlocal
