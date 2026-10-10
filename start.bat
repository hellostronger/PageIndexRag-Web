@echo off
chcp 65001 >nul
echo.
echo ========================================
echo   PageIndex Web - Starting...
echo ========================================
echo.

cd /d "%~dp0"

REM The bundled frontend is prebuilt under server\static, so the FastAPI
REM backend serves the whole UI on its own - no npm needed.
echo [1/1] Starting server on http://localhost:8001 ...
echo.
echo Open http://localhost:8001 in your browser.
echo Set your API key from the Settings page (top-right gear icon).
echo.

python -m uvicorn server.main:app --host 0.0.0.0 --port 8001

pause