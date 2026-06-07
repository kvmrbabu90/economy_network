@echo off
title EconGraph Dev Launcher

echo === EconGraph Dev Environment ===
echo Backend  : http://localhost:8101
echo Frontend : http://localhost:5180
echo.

:: Kill any stale processes on these ports
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8101" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5180" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1

:: Start backend
echo Starting backend (uvicorn port 8101)...
start "EconGraph Backend" cmd /k "cd /d C:\Users\konda\OneDrive\econgraph_repo && python -m uvicorn api.main:app --host :: --port 8101 --reload"

:: Brief pause so uvicorn binds before frontend tries to talk to it
timeout /t 3 /nobreak >nul

:: Start frontend
echo Starting frontend (vite port 5180)...
start "EconGraph Frontend" cmd /k "cd /d C:\Users\konda\OneDrive\econgraph_repo\web && npm run dev -- --port 5180"

echo.
echo Both servers launched in separate windows.
echo Open: http://localhost:5180
echo.
pause
