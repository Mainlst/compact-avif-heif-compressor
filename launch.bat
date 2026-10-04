@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "APP_PY=%~dp0.venv-win\Scripts\python.exe"

if exist "%APP_PY%" goto check_dependencies

where python3 >nul 2>nul
if errorlevel 1 goto try_launcher
python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto try_launcher
python3 -m venv "%~dp0.venv-win"
if errorlevel 1 goto setup_failed
goto check_dependencies

:try_launcher
where py >nul 2>nul
if errorlevel 1 goto python_missing
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_missing
py -3 -m venv "%~dp0.venv-win"
if errorlevel 1 goto setup_failed

:check_dependencies
"%APP_PY%" -c "from pathlib import Path; import hashlib; import tkinter, PIL, tkinterdnd2, pillow_heif; from PIL import features; root=Path.cwd(); expected=hashlib.sha256((root/'requirements.txt').read_bytes()).hexdigest(); marker=root/'.venv-win'/'.requirements.sha256'; raise SystemExit(0 if marker.is_file() and marker.read_text(encoding='ascii').strip()==expected and features.check('avif') and pillow_heif.libheif_info()['HEIF'] else 1)" >nul 2>nul
if not errorlevel 1 goto ready
echo Installing Image Compressor dependencies...
"%APP_PY%" -m pip install --disable-pip-version-check --only-binary=:all: -r "%~dp0requirements.txt"
if errorlevel 1 goto setup_failed
"%APP_PY%" -c "import tkinter, tkinterdnd2, pillow_heif; from PIL import features; raise SystemExit(0 if features.check('avif') and pillow_heif.libheif_info()['HEIF'] else 1)"
if errorlevel 1 goto avif_missing
"%APP_PY%" -c "from pathlib import Path; import hashlib; root=Path.cwd(); (root/'.venv-win'/'.requirements.sha256').write_text(hashlib.sha256((root/'requirements.txt').read_bytes()).hexdigest(), encoding='ascii')"
if errorlevel 1 goto setup_failed

:ready
if /I "%~1"=="--setup-only" exit /b 0
"%APP_PY%" "%~dp0app.py"
if errorlevel 1 goto app_failed
exit /b 0

:python_missing
echo Python 3.10 or newer was not found.
echo Install 64-bit Python 3 from https://www.python.org/downloads/windows/
echo Enable "Add Python to PATH", then run launch.bat again.
goto failed

:avif_missing
echo This Python installation does not have AVIF, HEIF or Tk support.
echo Use the official 64-bit Python 3 installer, then delete .venv-win and retry.
goto failed

:setup_failed
echo Setup failed. Check the error above and your internet connection.
goto failed

:app_failed
echo Image Compressor stopped with an error. See the message above.

:failed
if /I not "%~1"=="--setup-only" pause
exit /b 1
