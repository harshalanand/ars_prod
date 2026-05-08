@echo off
:: VV Article Working Portal - Smart Launcher
:: Finds Python automatically and runs the server
SETLOCAL ENABLEDELAYEDEXPANSION
title VV Portal - Starting...

echo.
echo  ================================================
echo   VV Article Working Portal - Smart Launcher
echo  ================================================
echo.

cd /d "%~dp0backend"

:: Try to find Python in common locations
SET PYTHON_EXE=

:: Check standard PATH first
python --version >nul 2>&1
IF NOT ERRORLEVEL 1 (
    SET PYTHON_EXE=python
    GOTO :found
)

:: Check Python 3.11 all-users install
IF EXIST "C:\Program Files\Python311\python.exe" (
    SET PYTHON_EXE=C:\Program Files\Python311\python.exe
    GOTO :found
)

:: Check Python 3.11 current user install
IF EXIST "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe" (
    SET PYTHON_EXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe
    GOTO :found
)

:: Check Python 3.11 other user paths
FOR /D %%U IN ("C:\Users\*") DO (
    IF EXIST "%%U\AppData\Local\Programs\Python\Python311\python.exe" (
        SET PYTHON_EXE=%%U\AppData\Local\Programs\Python\Python311\python.exe
        GOTO :found
    )
)

:: Check Python 3.10
IF EXIST "C:\Program Files\Python310\python.exe" (
    SET PYTHON_EXE=C:\Program Files\Python310\python.exe
    GOTO :found
)

:: Check Python 3.12
IF EXIST "C:\Program Files\Python312\python.exe" (
    SET PYTHON_EXE=C:\Program Files\Python312\python.exe
    GOTO :found
)

:: Check Windows Store Python
FOR /D %%A IN ("C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python3*") DO (
    IF EXIST "%%A" (
        SET PYTHON_EXE=%%A
        GOTO :found
    )
)

echo [ERROR] Python not found in any known location.
echo Please install Python from https://python.org
echo Make sure to check "Add Python to PATH"
pause
exit /b 1

:found
echo [OK] Found Python: !PYTHON_EXE!
"!PYTHON_EXE!" --version

:: Create venv if it doesn't exist
IF NOT EXIST "venv\Scripts\python.exe" (
    echo [..] Creating virtual environment...
    "!PYTHON_EXE!" -m venv venv
    IF ERRORLEVEL 1 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
    
    echo [..] Installing packages (this takes 1-2 minutes)...
    venv\Scripts\pip.exe install fastapi==0.110.0 "uvicorn[standard]==0.29.0" sqlalchemy==2.0.29 pydantic==2.7.0 pydantic-settings==2.2.1 "python-jose[cryptography]==3.3.0" "passlib[bcrypt]==1.7.4" bcrypt==4.0.1 python-multipart==0.0.9 openpyxl==3.1.2
    echo [OK] Packages installed
)

:: Create .env if missing
IF NOT EXIST ".env" (
    IF EXIST ".env.example" copy ".env.example" ".env" >nul
)

:: Get local IP
FOR /F "tokens=4 delims= " %%i IN ('route print ^| find " 0.0.0.0" 2^>nul') DO (
    SET LOCAL_IP=%%i
    GOTO :got_ip
)
:got_ip
IF "!LOCAL_IP!"=="" SET LOCAL_IP=localhost

echo.
echo  ================================================
echo   Portal running at:
echo   http://localhost:8000
echo   http://!LOCAL_IP!:8000
echo   API Docs: http://localhost:8000/docs
echo  ================================================
echo.
echo  Default login: retail.ops@v2kart.com / Admin@2026
echo.

:: Open browser after 3 seconds
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

:: Start the server
venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

echo.
echo Server stopped.
pause
