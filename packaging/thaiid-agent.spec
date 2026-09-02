# PyInstaller spec — one self-contained binary per OS.
#
# Run from the repo root:  pyinstaller packaging/thaiid-agent.spec
#
# A spec rather than a command line, because --add-data uses ':' on POSIX and
# ';' on Windows, and that difference is the usual reason a matrix build works
# on two platforms and quietly ships a broken third.
import os

root = os.path.abspath(os.path.join(os.getcwd()))

a = Analysis(
    [os.path.join(root, 'thaiid-agent.py')],
    pathex=[root],
    # The page is served from the binary itself, so it has to travel with it.
    datas=[(os.path.join(root, 'reader.html'), '.')],
    # pythaiidcard and pyscard are imported INSIDE functions so the agent can
    # start (and --selftest can run) without a reader attached. PyInstaller
    # analyses imports statically, so it would not see them at all.
    hiddenimports=[
        'pythaiidcard',
        'pythaiidcard.reader',
        'pythaiidcard.models',
        'pythaiidcard.exceptions',
        'smartcard',
        'smartcard.scard',
        'smartcard.System',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'streamlit', 'typer'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='thaiid-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
