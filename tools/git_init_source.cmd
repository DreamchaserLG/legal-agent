@echo off
REM ============================================================
REM  把远端仓库克隆成本机的「代码源目录」，之后拉取只需一条命令
REM  用法:  tools\git_init_source.cmd <仓库URL> [目标目录]
REM  默认目标: C:\legal\legal-src
REM ============================================================
setlocal
if "%~1"=="" (
  echo 用法: tools\git_init_source.cmd ^<仓库URL^> [目标目录]
  echo 例如: tools\git_init_source.cmd https://github.com/you/legal-agent.git
  exit /b 1
)
set URL=%~1
set DST=%~2
if "%DST%"=="" set DST=C:\legal\legal-src

if exist "%DST%\.git" (
  echo [提示] %DST% 已是 git 仓库，改为拉取
  git -C "%DST%" remote set-url origin "%URL%"
  git -C "%DST%" pull --ff-only
  goto done
)

echo 克隆 %URL% 到 %DST% ...
git clone "%URL%" "%DST%"
if errorlevel 1 (
  echo [错误] 克隆失败，请检查 URL 或访问凭据（私有库需要 PAT）。
  exit /b 1
)

:done
if not exist "%DST%\app\main.py" (
  echo [警告] 克隆结果里没有 app\main.py，仓库结构可能与本部署不一致，请人工确认。
)
echo.
echo 完成。以后更新代码只需执行:  tools\pull_and_refresh.cmd
endlocal
