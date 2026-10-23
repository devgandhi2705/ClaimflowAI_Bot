@echo off
cd /d %~dp0
title ClaimFlow AI - Dashboard
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8501"
.venv\Scripts\streamlit run dashboard\dashboard.py
