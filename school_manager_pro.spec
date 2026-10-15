# PyInstaller spec for Windows onedir release.
# Run from the project root: pyinstaller --clean --noconfirm school_manager_pro.spec
from pathlib import Path

project = Path(SPECPATH)
icon_path = project / "assets" / "school_manager.ico"
logo_path = project / "assets" / "school_manager_logo.png"
update_config_path = project / "update_config.json"

a = Analysis(
    [str(project / "school_manager.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(icon_path), "assets"), (str(logo_path), "assets"), (str(update_config_path), ".")],
    hiddenimports=["arabic_reshaper", "bidi", "bidi.algorithm"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SchoolManagerPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
    version=str(project / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SchoolManagerPro",
)
