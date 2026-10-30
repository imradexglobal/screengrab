@echo off
setlocal

:: Change to the batch file's directory
cd /d "%~dp0"

:: Simulate missing Python by using a non-existent path
set "PYTHON_EXE=C:\nonexistent\python.exe"

:: Verify Python executable exists
if not exist "%PYTHON_EXE%" (
    echo ERROR: Python executable not found.
    echo.
    echo Checked:
    echo   %PYTHON_EXE%
    echo.
    echo Install Python or correct the interpreter path.
    pause
    exit /b 1
)

:: Verify main.py exists
if not exist "%~dp0src\main.py" (
    echo ERROR: src\main.py was not found.
    echo.
    echo Confirm that this batch file is in the project root.
    pause
    exit /b 1
)

echo Starting Screen Monitor...
echo Using Python: "%PYTHON_EXE%"
echo.

"%PYTHON_EXE%" "%~dp0src\main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo Screen Monitor exited with error code %APP_EXIT_CODE%.
)

echo.
pause
exit /b %APP_EXIT_CODE%