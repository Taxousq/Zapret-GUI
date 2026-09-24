@echo off
chcp 65001 >nul
setlocal

rem ===========================================================================
rem  Zapret GUI - run from source
rem  Creates the venv virtual environment, installs dependencies and runs
rem  main.py.
rem
rem  Managing the zapret service requires administrator rights: start this
rem  file with right click -^> "Run as administrator".
rem
rem  All messages are ASCII on purpose: Cyrillic text in a .bat file breaks
rem  cmd.exe parsing under a non-UTF-8 console code page.
rem ===========================================================================

cd /d "%~dp0"

echo ============================================
echo   Zapret GUI - Run From Source
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.10+ and try again.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment venv...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment venv already exists.
)

echo.
echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate the virtual environment.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [3/3] Launching application...
echo.
python main.py

echo.
echo Application finished.
pause
endlocal
