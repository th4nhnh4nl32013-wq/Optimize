@echo off
REM ===============================================
REM  optimize.bat - command line launcher for Optimize
REM
REM  Usage:
REM    optimize <file.opt>     Run a .opt script
REM    optimize -help          Show help guide
REM    optimize -ver           Show Optimize version
REM    optimize                Start the Optimize interactive shell
REM
REM  Setup:
REM    1. Place this file and optimize.py in the same folder.
REM    2. Add that folder to your PATH (System Properties >
REM       Environment Variables > Path), or copy both files into
REM       a folder that's already on your PATH.
REM    3. Open a new cmd window and type: optimize -ver
REM ===============================================

setlocal
set SCRIPT_DIR=%~dp0

if "%~1"=="-help" goto :help
if "%~1"=="-ver" goto :ver

python "%SCRIPT_DIR%main.py" %*
goto :eof

:help
echo Optimize Language
echo.
echo Usage:
echo  optimize <file.opt>     Run a .opt script
echo  optimize -help          Show this help
echo  optimize -ver           Show version
goto :eof

:ver
echo Optimize version v0.1
goto :eof

endlocal