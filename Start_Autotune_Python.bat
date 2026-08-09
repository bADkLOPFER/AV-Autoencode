@echo off
chcp 65001 >nul
title AutoTune Python Starter

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

if "%~1"=="" (
    echo [INFO] Keinen Film übergeben. Starte interaktiven Modus...
    "%PYTHON_EXE%" -m video_tool.main
) else (
    echo [INFO] Verarbeite übergebene Datei: "%~nx1"
    "%PYTHON_EXE%" -m video_tool.main "%~1"
)

if errorlevel 1 (
    echo.
    echo [FEHLER] Skriptausführung fehlgeschlagen.
    pause
)
