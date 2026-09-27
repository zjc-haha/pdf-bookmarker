@echo off
cd /d "%~dp0"
python -m bookmarker.gui
if errorlevel 1 pause
