@echo off
:: ─────────────────────────────────────────────────────────────────────────────
:: ATLAS Windows build wrapper
::
:: Usage
:: ─────
::   build_windows.bat           standard build
::   build_windows.bat --clean   clean rebuild
::   build_windows.bat --debug   keep console window + verbose output
::   build_windows.bat --check   environment check only
:: ─────────────────────────────────────────────────────────────────────────────

setlocal EnableDelayedExpansion

echo === ATLAS Windows Builder ===
echo Platform: Windows %PROCESSOR_ARCHITECTURE%
echo.

:: Move to script directory
cd /d "%~dp0"

:: Forward all args to build.py
python build.py %*
set BUILD_STATUS=%ERRORLEVEL%

if "%BUILD_STATUS%"=="0" (
    if /I NOT "%~1"=="--check" (
        echo.
        echo === Post-build notes ===
        echo * Set runtime env vars before launching:
        echo     set GROQ_API_KEY=your_key_here
        echo   Or add them permanently in System Properties ^> Environment Variables.
        echo.
        echo * Optional native tools:
        echo     Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki
        echo     ffmpeg:        https://ffmpeg.org/download.html
        echo.
        echo * Output: dist\ATLAS\ATLAS.exe
    )
)

exit /b %BUILD_STATUS%
