@echo off
:: ============================================================
::  ARS v4 - run the servers: DEV, PROD or STOP
::
::  Usage:  START_SERVERS.cmd                 menu
::          START_SERVERS.cmd dev             DEV  - hot-reload backend + Vite dev server
::          START_SERVERS.cmd dev noreload    DEV  - no --reload, so a code edit does not
::                                                   kill a long Get Data load
::          START_SERVERS.cmd prod            PROD - build the frontend, backend serves it,
::                                                   one process, no reload
::          START_SERVERS.cmd stop            STOP - stop this checkout's servers
::
::  DEV  : backend :8080, frontend :3001 (proxies /api to the backend)
::  PROD : backend :8080 serves frontend\dist (via ARS_STATIC_DIR) and the API
::
::  If a default port is taken (e.g. ars_v3 is running), the next free port is
::  used and the frontend is pointed at THIS backend through API_TARGET.
::  STOP only touches processes whose command line contains this folder's path,
::  so ars_v3 / ars_prod servers are never stopped from here.
:: ============================================================
setlocal
title ARS v4 - Launcher

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "VENV_PY=%BACKEND%\venv\Scripts\python.exe"

set "MODE=%~1"
set "RELOAD=--reload --reload-dir app"
if /I "%~2"=="noreload" set "RELOAD="
if /I "%MODE%"=="noreload" (set "MODE=dev" & set "RELOAD=")
if defined MODE goto dispatch

:: ---- menu --------------------------------------------------
echo.
echo  ================================================
echo   ARS v4 - Servers
echo  ================================================
echo    1  DEV   hot-reload backend + Vite dev server
echo    2  DEV   no reload - safe for long Get Data loads
echo    3  PROD  build frontend, serve it from the backend
echo    4  STOP  stop this checkout's servers
echo    Q  Quit
echo.
choice /C 1234Q /N /M "  Select [1-4, Q]: "
if errorlevel 5 exit /b 0
if errorlevel 4 (set "MODE=stop" & goto dispatch)
if errorlevel 3 (set "MODE=prod" & goto dispatch)
if errorlevel 2 (set "MODE=dev" & set "RELOAD=" & goto dispatch)
set "MODE=dev"

:dispatch
echo.
if /I "%MODE%"=="dev"  goto do_dev
if /I "%MODE%"=="prod" goto do_prod
if /I "%MODE%"=="stop" goto do_stop
echo [ERROR] Unknown option "%MODE%". Use: dev, dev noreload, prod, stop
goto fail

:: ============================================================
:do_dev
title ARS v4 - DEV
call :need_python
if errorlevel 1 goto fail
call :need_node
if errorlevel 1 goto fail
call :offer_stop

call :free_port 8080 BPORT
call :free_port 3001 FPORT
if not "%BPORT%"=="8080" echo [WARN] Port 8080 is busy - backend will use %BPORT%
if not "%FPORT%"=="3001" echo [WARN] Port 3001 is busy - frontend will use %FPORT%

if defined RELOAD (echo [..] Starting backend on :%BPORT% with hot-reload) else (echo [..] Starting backend on :%BPORT% without reload)
start "ARS v4 DEV Backend :%BPORT%" /D "%BACKEND%" cmd /k ""%PY%" -m uvicorn main:app --app-dir "%BACKEND%" --host 0.0.0.0 --port %BPORT% %RELOAD%"

echo [..] Starting frontend on :%FPORT% - /api goes to :%BPORT%
set "API_TARGET=http://127.0.0.1:%BPORT%"
start "ARS v4 DEV Frontend :%FPORT%" /D "%FRONTEND%" cmd /k "npm --prefix "%FRONTEND%" run dev -- --port %FPORT% --strictPort"

call :wait_health %BPORT%
start "" "http://localhost:%FPORT%"

echo.
echo  ================================================
echo   DEV running
echo   Frontend : http://localhost:%FPORT%
echo   Backend  : http://localhost:%BPORT%
echo   API docs : http://localhost:%BPORT%/docs
echo  ================================================
echo   Stop: run this again and pick 4, or close the server windows.
echo.
pause
exit /b 0

:: ============================================================
:do_prod
title ARS v4 - PROD
call :need_python
if errorlevel 1 goto fail
call :need_node
if errorlevel 1 goto fail
call :offer_stop

echo [..] Building frontend - npm run build ...
pushd "%FRONTEND%"
call npm run build
set "BUILD_RC=%errorlevel%"
popd
if not "%BUILD_RC%"=="0" (
    echo [ERROR] Frontend build failed - see the output above.
    goto fail
)
if not exist "%FRONTEND%\dist\index.html" (
    echo [ERROR] Build finished but frontend\dist\index.html is missing.
    goto fail
)
echo [OK] Frontend built into frontend\dist

call :free_port 8080 BPORT
if not "%BPORT%"=="8080" echo [WARN] Port 8080 is busy - using %BPORT%

:: One process, no reload: the Report / SAP / Get Data schedulers start in the
:: app lifespan, so multiple workers would run every scheduled job N times.
echo [..] Starting backend on :%BPORT% - serving frontend\dist
set "ARS_STATIC_DIR=%FRONTEND%\dist"
start "ARS v4 PROD :%BPORT%" /D "%BACKEND%" cmd /k ""%PY%" -m uvicorn main:app --app-dir "%BACKEND%" --host 0.0.0.0 --port %BPORT%"

call :wait_health %BPORT%
start "" "http://localhost:%BPORT%"

echo.
echo  ================================================
echo   PROD running
echo   App      : http://localhost:%BPORT%
echo   API docs : http://localhost:%BPORT%/docs
echo  ================================================
echo   Stop: run this again and pick 4, or close the server window.
echo.
pause
exit /b 0

:: ============================================================
:do_stop
title ARS v4 - STOP
call :ars_procs stop
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1

:: ============================================================
::  helpers
:: ============================================================

:: need_python : sets PY to backend\venv if it has uvicorn, else the python on PATH
:need_python
set "PY="
if exist "%VENV_PY%" "%VENV_PY%" -c "import uvicorn" >nul 2>&1 && set "PY=%VENV_PY%"
if not defined PY python -c "import uvicorn" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [ERROR] No Python with uvicorn found - neither backend\venv nor python on PATH.
    echo         Install: backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    exit /b 1
)
echo [OK] Python: %PY%
if not exist "%BACKEND%\.env" echo [WARN] backend\.env is missing - copy backend\.env.example and fill in the DB settings.
exit /b 0

:: need_node : npm on PATH, node_modules installed
:need_node
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found on PATH. Install Node.js from https://nodejs.org
    exit /b 1
)
if exist "%FRONTEND%\node_modules" exit /b 0
echo [..] Installing frontend packages - first run only...
pushd "%FRONTEND%"
call npm install
set "NPM_RC=%errorlevel%"
popd
if not "%NPM_RC%"=="0" (
    echo [ERROR] npm install failed.
    exit /b 1
)
exit /b 0

:: offer_stop : if this checkout's servers are already up, offer to stop them first
:offer_stop
call :ars_procs check
if not errorlevel 1 exit /b 0
choice /C YN /M "  Stop them before starting"
if errorlevel 2 (
    echo [..] Leaving them running - new servers will take the next free ports.
    exit /b 0
)
call :ars_procs stop
ping -n 3 127.0.0.1 >nul
exit /b 0

:: ars_procs <check|stop> : find this checkout's server processes (cmd windows,
:: python/uvicorn, node/vite) by their command line containing ROOT.
:: check -> lists them, errorlevel 1 if any.  stop -> kills each process tree.
:ars_procs
set "ARS_ACTION=%~1"
set "ARS_ROOT=%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$r=$env:ARS_ROOT; $p=@(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and @('cmd.exe','python.exe','node.exe') -contains $_.Name -and $_.CommandLine -and $_.CommandLine.IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine -match 'uvicorn\s+main:app|vite|run dev' }); if($env:ARS_ACTION -eq 'check'){ if(-not $p.Count){ exit 0 }; Write-Host ('[WARN] ARS v4 servers already running ({0} processes):' -f $p.Count); $p | ForEach-Object { Write-Host ('         PID {0,-6} {1}' -f $_.ProcessId, $_.Name) }; exit 1 }; if(-not $p.Count){ Write-Host '[OK] No ARS v4 servers running.'; exit 0 }; foreach($x in $p){ Write-Host ('[..] Stopping PID {0,-6} {1}' -f $x.ProcessId, $x.Name); taskkill /PID $x.ProcessId /T /F *> $null }; Write-Host ('[OK] Stopped {0} ARS v4 server processes.' -f $p.Count)"
exit /b %errorlevel%

:: free_port <start> <outVar> : first port >= start with no LISTENING socket
:free_port
set /a "_p=%~1"
:free_port_loop
netstat -ano | findstr /C:":%_p% " | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    set /a "_p+=1"
    goto free_port_loop
)
set "%~2=%_p%"
exit /b 0

:: wait_health <port> : poll /health for up to 2 minutes
:wait_health
echo [..] Waiting for backend /health on :%~1 ...
set /a "TRIES=0"
:wait_health_loop
curl.exe -s -f -o nul "http://127.0.0.1:%~1/health" >nul 2>&1
if not errorlevel 1 goto wait_health_ok
set /a "TRIES+=1"
if %TRIES% GEQ 60 (
    echo [WARN] Backend not answering after 2 minutes - check its server window.
    exit /b 1
)
ping -n 3 127.0.0.1 >nul
goto wait_health_loop
:wait_health_ok
echo [OK] Backend is up:
curl.exe -s "http://127.0.0.1:%~1/health"
echo.
exit /b 0
