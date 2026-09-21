"""Entry-point для PyInstaller: собирает сайдкар в бинарь ape-sidecar.

Сборка (из desktop/):
    py -m PyInstaller --noconfirm build-sidecar.spec
Итог: dist/ape-sidecar/ape-sidecar(.exe) — кладётся в extraResources Electron.
"""
from sidecar.app import main

if __name__ == "__main__":
    main()
