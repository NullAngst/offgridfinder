# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for OffGridFinder.
#   pyinstaller --noconfirm --clean offgridfinder.spec
# Linux and Windows produce a single-file executable in dist/.
# macOS produces dist/OffGridFinder.app.
import re
import sys

from PyInstaller.utils.hooks import collect_all

with open("offgrid_finder.py", encoding="utf-8") as fh:
    VERSION = re.search(r'^APP_VERSION = "([^"]+)"', fh.read(), re.M).group(1)

# pyosmium is a C++ extension split over several submodules; take all of it.
osm_datas, osm_binaries, osm_hidden = collect_all("osmium")

datas = osm_datas + [
    ("assets/icon-256.png", "assets"),
    ("assets/icon.ico", "assets"),
]

a = Analysis(
    ["offgrid_finder.py"],
    pathex=[],
    binaries=osm_binaries,
    datas=datas,
    hiddenimports=osm_hidden,
    excludes=["matplotlib", "numpy", "pandas", "PIL", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="OffGridFinder",
        console=False,
        argv_emulation=False,
        icon="assets/icon.icns",
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="OffGridFinder")
    app = BUNDLE(
        coll,
        name="OffGridFinder.app",
        icon="assets/icon.icns",
        bundle_identifier="io.github.nullangst.offgridfinder",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.navigation",
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="OffGridFinder",
        console=False,
        upx=False,  # UPX-packed executables trip more antivirus heuristics
        icon="assets/icon.ico" if sys.platform == "win32" else None,
    )
