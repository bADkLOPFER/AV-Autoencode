@echo off
chcp 65001 >nul
title AutoTune PowerShell Starter

if "%~1"=="" (
    echo [INFO] Keinen Film übergeben. Starte interaktiven Modus...
    powershell.exe -ExecutionPolicy Bypass -File "%~dp0AutoTune_Encode.ps1"
) else (
    echo [INFO] Verarbeite übergebene Datei: "%~nx1"
    powershell.exe -ExecutionPolicy Bypass -File "%~dp0AutoTune_Encode.ps1" "%~1"
)

if errorlevel 1 (
    echo.
    echo [FEHLER] Skriptausführung fehlgeschlagen.
    pause
)