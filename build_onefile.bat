@echo off
REM ─────────────────────────────────────────────────────────────
REM  Build opus2gh → SATU file exe (onefile, tanpa folder _internal)
REM
REM  Hasil: dist\opus2gh.exe  (standalone, semua dependency terbundle)
REM
REM  Yang tetap di luar (dibuat otomatis di samping exe):
REM    settings.json  - konfigurasi user
REM    music\         - folder lagu user
REM    cookies.txt    - opsional anti-bot YouTube
REM    catalog.json   - hasil sync katalog repo
REM ─────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

set VENV_PY=.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo [ERROR] Python venv tidak ditemukan: %VENV_PY%
    echo         Jalankan dulu: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo [1/3] Cek PyInstaller...
"%VENV_PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo        install PyInstaller...
    "%VENV_PY%" -m pip install --quiet pyinstaller
)

echo [2/3] Build onefile exe...
"%VENV_PY%" -m PyInstaller opus2gh_onefile.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] Build gagal.
    exit /b 1
)

echo [3/3] Selesai.
echo.
echo  Hasil: dist\opus2gh.exe
dir /b dist\opus2gh.exe 2>nul && (
    for %%I in (dist\opus2gh.exe) do echo  Ukuran: %%~zI bytes
)
echo.
echo  Salin opus2gh.exe ke folder tujuan, jalankan, maka
echo  settings.json ^& music\ dibuat otomatis di sana.
endlocal
