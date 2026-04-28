@echo off
setlocal

rem Start QWBot web service for local Windows debugging.
rem Usage:
rem   scripts\dev-web.bat [test|prod] [port]

cd /d "%~dp0\.."

set "WEBHOOK_TARGET=%~1"
if "%WEBHOOK_TARGET%"=="" set "WEBHOOK_TARGET=test"

set "QWBOT_PORT_ARG=%~2"
if "%QWBOT_PORT_ARG%"=="" set "QWBOT_PORT_ARG=5000"

set "WECOM_WEBHOOK_TARGET=%WEBHOOK_TARGET%"
if not defined QWBOT_DB_PATH set "QWBOT_DB_PATH=data\qwbot.sqlite3"

if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)

echo Starting QWBot web service...
echo Webhook target: %WECOM_WEBHOOK_TARGET%
echo URL: http://127.0.0.1:%QWBOT_PORT_ARG%
echo Press Ctrl+C to stop.

python -m qwbot.cli web --host 127.0.0.1 --port %QWBOT_PORT_ARG%

endlocal
