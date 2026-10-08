# -*- mode: python ; coding: utf-8 -*-
# Compilar con:  python -m PyInstaller ClassHelper.spec --noconfirm --clean
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = []
# Modelo VAD (silero_vad_v6.onnx): sin él faster-whisper devuelve texto vacío en el .exe
datas += collect_data_files('faster_whisper')
datas += [('assets/icon.png', 'assets')]

binaries = []
binaries += collect_dynamic_libs('ctranslate2')
binaries += collect_dynamic_libs('onnxruntime')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        'onnxruntime',
        'google.generativeai',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'tensorflow', 'matplotlib', 'PyQt5', 'PyQt6', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClassHelper',
    icon='assets/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX rompe las DLL de onnxruntime/ctranslate2
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ClassHelper',
)
