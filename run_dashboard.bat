@echo off
setlocal
set PYTHONPATH=%~dp0src
echo Starting URL Analyzer Dashboard...
echo Open your browser at: http://localhost:8080
echo Press Ctrl+C to stop.
echo.
python dashboard.py %*
endlocal
