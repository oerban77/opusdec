# -*- mode: python ; coding: utf-8 -*-
"""
Spec PyInstaller — opus2gh sebagai SATU file exe (onefile).

Semua dependency (Python, modul opus2gh, ffmpeg/ffplay/ffprobe + DLL,
opus.dll untuk opuslib) di-bundle ke dalam 1 file `opus2gh.exe`.

Yang TETAP di luar (tidak dibundle):
  - settings.json   → konfigurasi user (dibuat otomatis di samping exe)
  - music/          → folder lagu user (dibuat otomatis di samping exe)
  - cookies.txt     → opsional, untuk anti-bot YouTube
  - catalog.json    → hasil sync katalog repo

Build:
    pyinstaller opus2gh_onefile.spec --clean --noconfirm
"""

import glob
import os

block_cipher = None

# ── Lokasi binary eksternal ────────────────────────────────────
# Sesuaikan path ini jika ffmpeg / opus.dll ada di lokasi lain.
FFMPEG_BIN = os.environ.get(
    'FFMPEG_BIN', r'D:\master\tools\ffmpeg\bin')
OPUS_DLL = os.environ.get(
    'OPUS_DLL', r'D:\master\tools\wireshark-portable\app\opus.dll')

binaries = []
for exe in ('ffmpeg.exe', 'ffplay.exe', 'ffprobe.exe'):
    p = os.path.join(FFMPEG_BIN, exe)
    if os.path.isfile(p):
        binaries.append((p, 'bin'))
    else:
        print(f'[WARN] tidak ditemukan: {p}')
for dll in glob.glob(os.path.join(FFMPEG_BIN, '*.dll')):
    binaries.append((dll, 'bin'))
if os.path.isfile(OPUS_DLL):
    binaries.append((OPUS_DLL, 'bin'))
else:
    print(f'[WARN] tidak ditemukan: {OPUS_DLL}')

a = Analysis(
    ['app.py'],
    pathex=['d:\\project\\embeded\\opusdec'],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        # opuslib memuat opus.dll via ctypes saat di-import
        'opuslib.api',
        'opuslib.exceptions',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy', 'PyQt5', 'PyQt6',
        'PySide2', 'PySide6', 'pytest', 'unittest', 'pip', 'setuptools',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='opus2gh',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI: tanpa window console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
