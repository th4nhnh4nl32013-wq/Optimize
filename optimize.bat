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
REM    1. Place this file, optimize.py, and repl.py in the same folder.
REM    2. Add that folder to your PATH (System Properties >
REM       Environment Variables > Path), or copy both files into
REM       a folder that's already on your PATH.
REM    3. Open a new cmd window and type: optimize -ver
REM ===============================================

setlocal
set SCRIPT_DIR=%~dp0

if "%~1"=="-help" goto :help
if "%~1"=="-ver" goto :ver
if "%~1"=="" goto :shell

python "%SCRIPT_DIR%main.py" %*
goto :eof

:shell
python "%SCRIPT_DIR%ide.py"
goto :eof

:help
echo Optimize Language - Syntax Guide
echo =================================
echo.
echo USAGE:
echo   optimize ^<file.opt^>     Run a .opt script
echo   optimize -help          Show this help
echo   optimize -ver           Show version
echo   optimize                Start the interactive shell
echo.
echo COMMENTS:
echo   ! this is a comment
echo.
echo VARIABLES:
echo   x = 5                   Bare assignment (auto scope)
echo   var x = 5               Same as bare assignment
echo   local x = 5             Local scope assignment
echo   global x = 5            Global scope assignment
echo   x += 1                  Compound assignment (also -=, *=, /=)
echo.
echo OUTPUT:
echo   display "Hello"         Print a value
echo   type x                  Print the type of a value
echo.
echo LISTS:
echo   list a = [1, 2, 3]      Declare a list
echo   add a 4                 Append a value to a list
echo   del a 4                 Remove a value from a list
echo   del a[0]                Remove by index
echo   a[last_item]            Access the last element
echo   a.start()               First valid index (0)
echo   a.end()                 One past the last index (len)
echo.
echo INPUT:
echo   input name              Read a string
echo   input int age           Read and convert to int
echo   input float f           Read and convert to float
echo   input bool b            Read and convert to bool (true/false)
echo   input list l            Read and convert to list
echo   input str s "Prompt: "  Read with a custom prompt
echo.
echo CONDITIONALS:
echo   if (x ^< 5):
echo       display "small"
echo   elseif (x == 5):
echo       display "equal"
echo   else:
echo       display "big"
echo   end
echo.
echo LOOPS:
echo   for (i = 0, i += 1, i ^< 10):
echo       display i
echo   end
echo.
echo   while (x ^< 10):
echo       x += 1
echo   end
echo.
echo   next                    Skip to next iteration (continue)
echo   stop                    Exit the loop (break)
echo.
echo FUNCTIONS:
echo   function add(a, b):
echo       return a + b
echo   end
echo.
echo   add(2, 3)               Call a function
echo   escape                  No-op / early exit marker
echo.
echo LIBRARIES:
echo   library optmath         Load a built-in or custom library
echo   library algorithm
echo   library optrand
echo   library optstr
echo   library opttime
echo.
echo LITERALS:
echo   "text" or 'text'        Strings
echo   True / False            Booleans
echo   [1, 2, 3]                Lists
echo   123 / 4.5                Integers / Floats
goto :eof

:ver
echo Optimize version v0.1
goto :eof

endlocal