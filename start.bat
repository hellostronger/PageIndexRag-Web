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
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

REM Python 3.10.0 ships a typing.ForwardRef without the is_class argument
REM pydantic passes, so litellm fails to import on it.
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo [X] Python 3.11+ is required ^(3.10.0 will not work^).
    echo     Current: 
    %PYTHON% --version
    pause
    exit /b 1
)

echo [1/1] Starting server on http://localhost:8001 ...
echo.
echo Open http://localhost:8001 in your browser.
echo Set your API key from the Settings page (top-right gear icon).
echo.

%PYTHON% -m uvicorn server.main:app --host 0.0.0.0 --port 8001

pause