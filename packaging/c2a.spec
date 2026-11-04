# PyInstaller spec: single-folder `c2a` binary (onedir starts faster than onefile).
# Build: pyinstaller packaging/c2a.spec --distpath dist --workpath build
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["../c2a/__main__.py"],
    pathex=[".."],
    datas=collect_data_files("c2a"),
    hiddenimports=["c2a.board.sync", "c2a.board.server", "c2a.board.projector"],
    excludes=["tkinter", "pytest", "hypothesis", "mypy", "ruff"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="c2a", strip=True, upx=False,
          console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=True, upx=False, name="c2a")
