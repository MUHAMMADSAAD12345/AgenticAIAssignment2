@echo off
REM Offline tests + full end-to-end test against the LOCAL MOCK server (no real game, no LLM).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
call .venv\Scripts\activate.bat
python -m scripts.verify_local
python -m tests.e2e_mock
pause
