@echo off
setlocal
title ScreenMonitor Build

echo ============================================================
echo   ScreenMonitor - rebuild the executable
echo ============================================================

cd /d "%~dp0"

REM --- Parse optional arguments --------------------------------
set RUN_TESTS=1
set LAUNCH=0
:parse
if "%~1"=="" goto :parsed
if /I "%~1"=="/fast"      set RUN_TESTS=0
if /I "%~1"=="/skip-tests" set RUN_TESTS=0
if /I "%~1"=="/run"        set LAUNCH=1
shift
goto :parse
:parsed

REM --- Prerequisites -------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    goto :fail
)
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed. Run first:
    echo         python -m pip install pyinstaller
    goto :fail
)

REM --- Close a running copy so dist files are not locked -------
tasklist /FI "IMAGENAME eq ScreenMonitor.exe" 2>nul | find /I "ScreenMonitor.exe" >nul
if not errorlevel 1 (
    echo.
    echo ScreenMonitor.exe is currently running.
    choice /C YN /N /M "Close it now and rebuild? [Y/N] "
    if errorlevel 2 (
        echo Cancelled - close the app yourself, then run this script again.
        goto :fail
    )
    taskkill /F /IM ScreenMonitor.exe >nul 2>&1
    timeout /t 2 /nobreak >nul
)

REM --- 1. Tests -------------------------------------------------
echo.
if "%RUN_TESTS%"=="1" (
    echo [1/3] Running the test suite...
    python -m pytest tests -q
    if errorlevel 1 (
        echo.
        echo [ERROR] Tests failed - build aborted so a broken exe is not shipped.
        echo         Fix the failures, or rebuild without tests:  build_exe.bat /fast
        goto :fail
    )
) else (
    echo [1/3] Tests skipped ^(fast mode^).
)

REM --- 2. Build -------------------------------------------------
echo.
echo [2/3] Building with PyInstaller ^(this can take a few minutes^)...
python -m PyInstaller --noconfirm --clean ScreenMonitor.spec
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller failed - see the messages above.
    goto :fail
)

REM --- 3. Verify ------------------------------------------------
echo.
echo [3/3] Verifying the output...
if not exist "dist\ScreenMonitor\ScreenMonitor.exe" (
    echo [ERROR] dist\ScreenMonitor\ScreenMonitor.exe was not produced.
    goto :fail
)
for %%F in ("dist\ScreenMonitor\ScreenMonitor.exe") do (
    echo.
    echo ============================================================
    echo   BUILD OK
    echo   File:  %%~F
    echo   Size:  %%~zF bytes
    echo   Built: %%~tF
    echo ============================================================
)
echo.
echo Your data is never touched by a build:
echo   %APPDATA%\ScreenMonitor\screen_monitor.db
echo.
echo If Windows shows a SmartScreen warning on first run, click
echo "More info" then "Run anyway" ^(the exe is not code-signed^).
echo.

if "%LAUNCH%"=="1" (
    echo Starting the new build...
    start "" "%~dp0dist\ScreenMonitor\ScreenMonitor.exe"
)

pause
exit /b 0

:fail
echo.
echo BUILD NOT COMPLETED.
pause
exit /b 1
