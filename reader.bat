@echo off
rem ============================================================
rem  terminal_reader launcher
rem  (this file is intentionally pure ASCII so it never garbles)
rem
rem  Usage:
rem     reader.bat                     open the shelf (recent books)
rem     reader.bat D:\books\abc.txt    open a specific novel
rem     reader.bat --selftest          run the non-interactive self test
rem     reader.bat --probe             report terminal capabilities
rem
rem  Tip: you can also drag a .txt file and drop it onto this .bat
rem ============================================================
setlocal
set "HERE=%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [ERROR] Python 3 not found in PATH.
  pause
  exit /b 1
)

set "ARG1=%~1"
if "%ARG1:~0,2%"=="--" (
  %PY% "%HERE%terminal_reader.py" %*
  goto :done
)

if "%ARG1%"=="" (
  %PY% "%HERE%terminal_reader.py"
) else (
  %PY% "%HERE%terminal_reader.py" "%~1"
)

:done
if errorlevel 1 (
  echo.
  echo [exit code: %errorlevel%]
  pause
)
endlocal
