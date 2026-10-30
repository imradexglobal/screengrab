@echo off
setlocal

:: Change to the batch file's directory
cd /d "%~dp0"

:: Use valid Python path
set "PYTHON_EXE=C:\Program Files\Python312\python.exe"

:: Temporarily rename main.py to test error handling
if exist "%~dp0src\main.py" (
    rename "%~dp0src\main.py" "__mainpybackup__.py"
)

:: Verify main.py exists (it won't now)
if not exist "%~dp0src\main.py" (
    echo ERROR: src\main.py was not found.
    echo.
    echo Confirm that this batch file is in the project root.
    pause
    exit /b 1
)

:: Restore main.py
if exist "%~dp0__mainpybackup__.py" (
    rename "%~dp0__mainpybackup__.py" "%~dp0src\main.py"
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