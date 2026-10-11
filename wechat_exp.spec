# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[('src', 'src'), ('README.md', '.'), ('tools/silk_decoder.exe', 'tools')],
    hiddenimports=['Crypto.Cipher.AES', 'Crypto.Util.Padding', 'flask', 'werkzeug', 'jinja2', 'markdown', 'blackboxprotobuf', 'zstandard', 'openpyxl', 'jieba', 'pypinyin', 'psutil', 'requests'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='wechat_exp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
