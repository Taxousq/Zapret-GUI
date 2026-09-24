@echo off
chcp 65001 >nul
setlocal

rem ===========================================================================
rem  Zapret GUI - build exe (PyInstaller)
rem  Run: build.bat   (double click, or ".\build.bat" from PowerShell)
rem  Result: dist\ZapretGUI.exe
rem
rem  NOTE: PyInstaller cannot cross-compile - the exe can only be built on
rem  Windows (same architecture as the target system).
rem  The zapret folder is NOT bundled: it is downloaded on first launch.
rem
rem  All messages are ASCII on purpose: Cyrillic text in a .bat file breaks
rem  cmd.exe parsing under a non-UTF-8 console code page.
rem ===========================================================================

cd /d "%~dp0"

echo ============================================
echo   Zapret GUI - Build Script
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.10+ and try again.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [2/3] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
)

echo.
echo [3/3] Building executable...
python -m PyInstaller --clean --noconfirm zapret-gui.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
if exist "dist\ZapretGUI.exe" (
    echo [OK] Build complete: dist\ZapretGUI.exe
) else (
    echo [ERROR] Build finished but dist\ZapretGUI.exe not found.
    echo Check the output above.
    pause
    exit /b 1
)

echo.
echo Next steps:
echo   * copy the exe next to portable.flag for the portable version;
echo   * build the installer from installer.iss with Inno Setup.
echo.

pause
endlocal
