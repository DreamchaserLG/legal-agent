@echo off
REM ============================================================
REM  拉取最新版代码 + 刷新本机部署（一条命令搞定）
REM  用法:  tools\pull_and_refresh.cmd [git源目录]
REM  默认源目录: C:\legal\legal-src
REM ============================================================
setlocal
set SRC=%~1
if "%SRC%"=="" set SRC=C:\legal\legal-src
set PY=C:\miniconda3\envs\legal\python.exe

if not exist "%SRC%\.git" (
  echo [错误] 未找到 git 源目录: %SRC%
  echo        先执行: tools\git_init_source.cmd ^<仓库URL^>
  exit /b 1
)

echo [1/3] git pull --ff-only %SRC%
git -C "%SRC%" pull --ff-only
if errorlevel 1 (
  echo [错误] 拉取失败（可能有本地改动或需要冲突处理），已终止，部署未被修改。
  exit /b 1
)

echo [2/3] 刷新部署目录（备份 - 替换 - 自检 - 重启 - 健康校验）
"%PY%" "%~dp0refresh_deploy.py" --source "%SRC%" --restart
if errorlevel 1 (
  echo [错误] 刷新失败，已自动回滚。详情见 logs\ 与 runtime\last_refresh.json
  exit /b 1
)

echo [3/3] 当前数据源状态
"%PY%" "%~dp0db_source.py" status
endlocal
