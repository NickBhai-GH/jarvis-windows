@echo off
rem Starts Jarvis (in a hidden console, so the Claude processes it spawns never flash a window) and the widget.
cd /d "%~dp0"
start "" conhost.exe --headless "%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0jarvis.py"
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0widget.py"
start "" /d "%~dp0jarvis-os" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0jarvis-os\jarvis_os.py"
