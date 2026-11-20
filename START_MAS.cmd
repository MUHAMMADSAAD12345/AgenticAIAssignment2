@echo off
REM Starts the two A2A worker services and the coordinator in three separate windows.
REM Usage:  START_MAS.cmd            (assessed run, LLM planning if GROQ_API_KEY is set in this window)
REM         START_MAS.cmd --no-llm   (rule-based planning only)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv\Scripts\activate.bat (
  echo Create the virtual environment first: py -3.11 -m venv .venv
  exit /b 1
)
start "MAS mining agent :9999" cmd /k ".venv\Scripts\activate.bat && python -m mas.agents.mining_agent --port 9999"
start "MAS trading agent :9998" cmd /k ".venv\Scripts\activate.bat && python -m mas.agents.trading_agent --port 9998"
timeout /t 6 /nobreak >nul
start "MAS coordinator" cmd /k ".venv\Scripts\activate.bat && python -m mas.coordinator --assessed %*"
