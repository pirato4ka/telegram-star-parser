# -*- mode: python ; coding: utf-8 -*-
"""Сборка stars_parser.exe через PyInstaller: `pyinstaller stars_parser.spec --noconfirm`."""

from PyInstaller.utils.hooks import collect_submodules

# Telethon подгружает TL-схемы динамически — без этого exe падает на старте.
hiddenimports = collect_submodules("telethon")

block_cipher = None


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # Пример конфигурации попадает в каталог с exe: достаточно переименовать в conf.ini.
    datas=[("conf.example.ini", "."), ("README.md", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "PIL",
        "pytest",
        "IPython",
        "jupyter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="stars_parser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
