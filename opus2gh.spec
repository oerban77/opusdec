# -*- mode: python ; coding: utf-8 -*-

import os
import glob

ffmpeg_bin = r'D:\master\tools\ffmpeg\bin'
wireshark_app = r'D:\master\tools\wireshark-portable\app'

binaries = []
# Add the three executables
for exe in ['ffmpeg.exe', 'ffplay.exe', 'ffprobe.exe']:
    binaries.append((os.path.join(ffmpeg_bin, exe), 'bin'))
# Add all DLLs from ffmpeg_bin
for dll in glob.glob(os.path.join(ffmpeg_bin, '*.dll')):
    binaries.append((dll, 'bin'))
# Add opus.dll from wireshark
binaries.append((os.path.join(wireshark_app, 'opus.dll'), 'bin'))

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=['d:\\project\\embeded\\opusdec'],
    binaries=binaries,
    datas=[
        ('opus2gh', 'opus2gh'),
        # Note: We do not include the music folder because it is user data.
        # If you want to include an empty music folder, you can add:
        # ('music', 'music'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data,
          cipher=block_cipher)

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
    console=False,  # No console window for GUI
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='opus2gh',
)