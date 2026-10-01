@echo off
setlocal

set PYTHONPATH=%~dp0src
python -m url_analyzer %*

endlocal
