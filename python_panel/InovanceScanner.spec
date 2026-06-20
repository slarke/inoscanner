# -*- mode: python ; coding: utf-8 -*-
# Сборка:  pyinstaller InovanceScanner.spec
# Запускать из каталога python_panel. Результат: dist/InovanceScanner/.
#
# Ключевой момент: пакет servo_core (`core/`) лежит на уровень выше
# (в корне репозитория) и импортируется в queue_backbone.py через
# sys.path-хак, невидимый статическому анализатору PyInstaller. Поэтому
# корень репозитория добавляется в pathex и в sys.path — тогда PyInstaller
# анализирует core и сам тянет sqlite3, а hiddenimports подстраховывают.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent           # …/mvc_rust_source (содержит core/)

# `from core import …` анализируется статически (см. pathex ниже) и тянет всю
# нужную часть core. НЕ используем collect_submodules('core'): оно затянуло бы
# core.qt_bridge / core.pyqt_example, которые завязаны на PyQt6 (конфликт с
# PyQt5). sqlite3 указываем явно — его прячет sys.path-хак в queue_backbone.
hiddenimports = ["sqlite3", "_sqlite3"]
hiddenimports += collect_submodules("pymodbus")       # у pymodbus много динамических импортов

a = Analysis(
    ["main.py"],
    pathex=[str(REPO_ROOT)],                           # <-- даёт PyInstaller увидеть `core`
    binaries=[],
    datas=[("app_config.json", ".")],                  # стартовые настройки рядом с exe
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6"],                                # приложение на PyQt5; PyQt6 не смешивать
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="InovanceScanner",
    console=False,            # True — если нужно видеть трассировки при отладке
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="InovanceScanner",
)
