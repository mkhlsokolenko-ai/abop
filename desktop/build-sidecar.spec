# PyInstaller spec: сборка Python-сайдкара в автономный бинарь ape-sidecar (onedir).
# Сборка из desktop/:  py -m PyInstaller --noconfirm build-sidecar.spec
# Итог: dist/ape-sidecar/  → кладётся в extraResources Electron (пользователю Python не нужен).
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("uvicorn", "fastapi", "starlette", "pydantic", "pydantic_core", "anyio",
            "docx", "openpyxl", "rapidocr_onnxruntime", "onnxruntime", "PIL", "sidecar"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# модули сайдкара — чтобы реестр нашёл их и в замороженной сборке
hiddenimports += collect_submodules("sidecar.modules")

a = Analysis(
    ["run_sidecar.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="ape-sidecar", console=False,  # окно консоли не всплывает; stdout идёт в пайп Electron
)
coll = COLLECT(exe, a.binaries, a.datas, name="ape-sidecar")
