@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:8501
python -m streamlit run swipe_triage.py
pause
