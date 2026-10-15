@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Build Madrasati Pro for Windows
set "LOG=%CD%\build_log.txt"

rem Write the log header one command at a time. This avoids CMD parsing errors
rem when the project path contains parentheses, such as "Fixed_Build (1)".
> "!LOG!" echo ============================================================
>> "!LOG!" echo Madrasati Pro - Windows Build Log
>> "!LOG!" echo Date: !DATE! !TIME!
>> "!LOG!" echo Project folder: !CD!
>> "!LOG!" echo ============================================================

echo ============================================================
echo   Madrasati Pro - Windows Build and Installer
echo ============================================================
echo A detailed log will be saved in build_log.txt.

echo Checking Python...
where python >> "!LOG!" 2>&1
if errorlevel 1 (
  echo [ERROR] Python was not found. Install Python 3.11 or 3.12 and enable Add Python to PATH.>> "!LOG!"
  goto :error
)
python --version >> "!LOG!" 2>&1
python -c "import sys; print('Python executable:', sys.executable); print('Python version:', sys.version)" >> "!LOG!" 2>&1

if not exist ".venv-build\Scripts\python.exe" (
  echo [1/6] Creating isolated build environment...
  python -m venv .venv-build >> "!LOG!" 2>&1
  if errorlevel 1 goto :error
)

if not exist ".venv-build\Scripts\python.exe" (
  echo [ERROR] The build virtual environment was not created.>> "!LOG!"
  goto :error
)

echo [2/6] Installing build tools...
".venv-build\Scripts\python.exe" -m pip install --upgrade pip >> "!LOG!" 2>&1
if errorlevel 1 goto :error
".venv-build\Scripts\python.exe" -m pip install -r requirements-build.txt >> "!LOG!" 2>&1
if errorlevel 1 goto :error

echo [3/6] Checking source syntax...
".venv-build\Scripts\python.exe" -m py_compile school_manager.py >> "!LOG!" 2>&1
if errorlevel 1 goto :error

echo [4/6] Running database smoke tests...
".venv-build\Scripts\python.exe" -m unittest discover -s tests -v >> "!LOG!" 2>&1
if errorlevel 1 goto :error

echo [5/6] Building Windows application folder...
".venv-build\Scripts\python.exe" -m PyInstaller --clean --noconfirm school_manager_pro.spec >> "!LOG!" 2>&1
if errorlevel 1 goto :error

if not exist "dist\SchoolManagerPro\SchoolManagerPro.exe" (
  echo [ERROR] PyInstaller finished but the expected EXE was not found.>> "!LOG!"
  goto :error
)

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC (
  echo.
  echo [WARNING] The application EXE was built, but Inno Setup 6 was not found.
  echo Install Inno Setup 6, then run this file again to create the installer.
  echo App EXE: dist\SchoolManagerPro\SchoolManagerPro.exe
  echo Log: build_log.txt
  echo Inno Setup download: https://jrsoftware.org/isdl.php
  echo Inno Setup 6 was not found in either standard installation folder.>> "!LOG!"
  pause
  exit /b 2
)

echo [6/6] Creating Windows setup installer...
"!ISCC!" installer\SchoolManagerPro.iss >> "!LOG!" 2>&1
if errorlevel 1 goto :error

if not exist "dist-installer\SchoolManagerPro_Setup_*.exe" (
  echo [ERROR] Inno Setup finished but the expected installer was not found.>> "!LOG!"
  goto :error
)

echo.
echo BUILD SUCCESSFUL
echo App folder: dist\SchoolManagerPro\
echo Installer: dist-installer\SchoolManagerPro_Setup_*.exe
echo Log: build_log.txt
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed. The detailed error is shown below and saved to build_log.txt.
echo ============================================================
type "!LOG!"
echo ============================================================
echo If you need help, send build_log.txt or a screenshot of this full error text.
pause
exit /b 1
