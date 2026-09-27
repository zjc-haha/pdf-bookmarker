"""Build a shared onedir bundle with a windowed GUI and console worker."""

from pathlib import Path


project = Path(SPECPATH).resolve().parent
icon_assets = sorted((project / "bookmarker" / "assets" / "icons").glob("*.png"))
if not icon_assets:
    raise FileNotFoundError("No bundled Tabler icon PNG files found")
analysis = Analysis(
    [str(project / "bookmarker" / "app.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(icon), "bookmarker/assets/icons") for icon in icon_assets],
    hiddenimports=["pypdfium2"],
    hookspath=[str(project / "packaging" / "hooks")],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
gui = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PDF书签工具",
    console=False,
    debug=False,
    strip=False,
    upx=False,
)
worker = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PDF书签命令行",
    console=True,
    debug=False,
    strip=False,
    upx=False,
)
COLLECT(
    gui,
    worker,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="PDF书签工具",
)
