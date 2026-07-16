@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Greska: Python nije pronadjen.
        echo Instaliraj Python 3.10 ili noviji i ukljuci "Add Python to PATH".
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo Provjeravam i instaliram requirements...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Pokrecem Aurora IPTV...
%PYTHON% main.py
if errorlevel 1 goto :error

exit /b 0

:error
echo.
echo Aurora IPTV nije mogla biti pokrenuta. Pogledaj gresku iznad.
pause
exit /b 1
