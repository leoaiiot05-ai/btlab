@echo off
rem Starts btlab in this console from the venv with config.toml; Ctrl+C stops it (SPEC.md section 23).
rem No labels or goto: cmd.exe runs this the same with LF or CRLF line endings.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The venv is missing: follow the install steps in README.md first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m btlab --config config.toml
rem A non-zero exit (2: config.toml or taxes.toml, 3: btlab already runs) keeps the window open to read it.
if errorlevel 1 pause
