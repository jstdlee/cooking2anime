# PyInstaller spec: single-folder `c2a` binary (onedir starts faster than onefile).
# Build: pyinstaller packaging/c2a.spec --distpath dist --workpath build
# The Studio frontend must be built first (studio/: npm ci && npm run build), so that
# c2a/studio/static is bundled.
# strip=False: stripping breaks the page alignment of bundled wheels' shared libraries
# (e.g. numpy's OpenBLAS) on aarch64, which then fail to load.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("uvicorn") + collect_submodules("websockets")
          + ["c2a.studio.server", "multipart", "python_multipart"])

a = Analysis(
    ["../c2a/__main__.py"],
    pathex=[".."],
    datas=collect_data_files("c2a"),
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "hypothesis", "mypy", "ruff"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="c2a", strip=False, upx=False,
          console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="c2a")
