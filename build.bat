@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
call "%~dp0launch.bat" --setup-only
if errorlevel 1 goto failed

set "APP_PY=%~dp0.venv-win\Scripts\python.exe"
echo Installing build dependencies...
"%APP_PY%" -m pip install --disable-pip-version-check --only-binary=:all: -r "%~dp0requirements-dev.txt"
if errorlevel 1 goto failed

if not exist "%~dp0vendor\libavif\windows\avifenc.exe" (
    echo Missing bundled AVIF encoder: vendor\libavif\windows\avifenc.exe
    goto failed
)
if not exist "%~dp0vendor\ffmpeg\windows\ffmpeg.exe" (
    echo Missing bundled hardware encoder: vendor\ffmpeg\windows\ffmpeg.exe
    goto failed
)
echo Building the portable ImageCompressor folder...
"%APP_PY%" -m PyInstaller --noconfirm --clean "%~dp0ImageCompressor.spec"
if errorlevel 1 goto failed

echo Building the optional ImageCompressor-color.exe...
"%APP_PY%" -m PyInstaller --noconfirm --clean "%~dp0ImageCompressor-color.spec"
if errorlevel 1 goto failed

echo Creating the portable ZIP...
"%APP_PY%" "%~dp0packaging\create_portable_zip.py"
if errorlevel 1 goto failed

echo.
echo Portable ZIP: %~dp0dist\ImageCompressor.zip
echo Portable launcher: %~dp0dist\ImageCompressor\ImageCompressor.exe
echo Optional single EXE: %~dp0dist\ImageCompressor-color.exe
pause
exit /b 0

:failed
echo Build failed. Check the error above.
pause
exit /b 1
