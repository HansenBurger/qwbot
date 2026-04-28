@echo off
setlocal

rem Test QWBot scheduled reminder on Windows.
rem Usage:
rem   scripts\test-scheduled.bat [once|send|preview] [test|prod]
rem
rem once    Run scheduled logic with business-day checks. Default.
rem send    Force send scheduled reminder.
rem preview Print scheduled reminder without sending.

cd /d "%~dp0\.."

set "MODE=%~1"
if "%MODE%"=="" set "MODE=once"

set "WEBHOOK_TARGET=%~2"
if "%WEBHOOK_TARGET%"=="" set "WEBHOOK_TARGET=test"

set "WECOM_WEBHOOK_TARGET=%WEBHOOK_TARGET%"
if not defined QWBOT_DB_PATH set "QWBOT_DB_PATH=data\qwbot.sqlite3"

if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)

if /I "%MODE%"=="once" (
  set "COMMAND=run-scheduled-once"
) else if /I "%MODE%"=="send" (
  set "COMMAND=send-now"
) else if /I "%MODE%"=="preview" (
  set "COMMAND=preview"
) else (
  echo Unknown mode: %MODE%
  echo Usage: scripts\test-scheduled.bat [once^|send^|preview] [test^|prod]
  exit /b 1
)

echo Running scheduled reminder test...
echo Mode: %MODE%
echo Webhook target: %WECOM_WEBHOOK_TARGET%

python -m qwbot.cli %COMMAND% --webhook %WEBHOOK_TARGET%

endlocal
