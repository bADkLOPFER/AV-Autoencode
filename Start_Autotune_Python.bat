@echo off
:: Start script für Windows
echo --- Starting Application ---

:: Check for venv
IF NOT EXIST "venv" (
    echo [ERROR] Virtual environment 'venv' nicht gefunden. Bitte erst deployen!
    pause
    exit /b
)

:: Activate and run
call venv\Scripts\activate
python -m uvicorn server:app --host 0.0.0.0 --port 8000
pause