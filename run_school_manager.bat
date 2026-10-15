@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 school_manager.py
if errorlevel 1 python school_manager.py
pause
