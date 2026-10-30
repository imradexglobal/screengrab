@echo off
setlocal EnableDelayedExpansion

:: Change to the repository root directory
cd /d "%~dp0"
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

:: Set PYTHONPATH to project root
set "PYTHONPATH=%PROJECT_ROOT%"

:: Determine Python executable
set "PYTHON_EXE="

:: 1. Check for virtual environment in project root
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_ROOT%\.venv\Scripts\python.exe"
)

:: 2. Check system Python 3.12 default install path
if not defined PYTHON_EXE (
    if exist "C:\Program Files\Python312\python.exe" (
        set "PYTHON_EXE=C:\Program Files\Python312\python.exe"
    )
)

:: 3. Check for python on system PATH
if not defined PYTHON_EXE (
    for /f "tokens=*" %%i in ('where python 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%i"
    )
)

:: 4. Check for py launcher
if not defined PYTHON_EXE (
    for /f "tokens=*" %%i in ('where py 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%i"
    )
)

:: Verify Python executable was found
if not defined PYTHON_EXE (
    echo ====================================================
    echo ERROR: Python executable could not be found.
    echo Please install Python 3.10+ or check your PATH.
    echo ====================================================
    pause
    exit /b 1
)

:: Verify src\main.py exists
if not exist "%PROJECT_ROOT%\src\main.py" (
    echo ====================================================
    echo ERROR: src\main.py was not found.
    echo Please run this batch file from the repository root.
    echo ====================================================
    pause
    exit /b 1
)

echo Starting Screen Monitor Application...
echo Using Python: "%PYTHON_EXE%"
echo Project Root: "%PROJECT_ROOT%"
echo.

"%PYTHON_EXE%" "%PROJECT_ROOT%\src\main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo Screen Monitor exited with error code %APP_EXIT_CODE%.
    echo.
    pause
)

exit /b %APP_EXIT_CODE%
