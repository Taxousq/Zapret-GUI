# -*- mode: python ; coding: utf-8 -*-
"""Конфигурация PyInstaller для Zapret GUI.

Сборка (только на Windows, PyInstaller не кросс-компилирует)::

    build.bat
    rem или вручную:
    python -m PyInstaller --clean --noconfirm zapret-gui.spec

Результат — один файл ``dist\\ZapretGUI.exe`` (GUI без окна консоли).

Что важно знать:

* ``hiddenimports``: ``PyQt6.QtCharts`` подключается динамически (график пинга
  на дашборде), поэтому без него exe упал бы при импорте. ``PyQt6.QtSvg``
  добавлен на случай, если тема/иконки начнут использовать SVG — пакет
  входит в PyQt6 и лишним не будет.
* ``datas=[]``: папка запрета в поставку **не** входит — она скачивается при
  первом запуске или указывается вручную в разделе «Настройки».
* ``console=False``: приложение графическое, консоль не нужна.
* ``icon``: если положить ``assets/icon.ico``, иконка подхватится сама;
  без файла сборка идёт с иконкой PyInstaller по умолчанию (файл не нужен).
* ``upx=True``: сжатие exe, если в PATH есть UPX; без UPX PyInstaller просто
  пропускает этот шаг.
"""

from pathlib import Path

# Каталог со spec-файлом: PyInstaller запускает его как обычный Python-скрипт,
# поэтому пути считаем от него, а не от текущей папки. В старых версиях
# PyInstaller SPECPATH не задан — тогда берём текущую папку.
try:
    project_dir = Path(SPECPATH).resolve()  # noqa: F821 — задаёт PyInstaller
except NameError:  # pragma: no cover — зависит от версии PyInstaller
    project_dir = Path.cwd()

# Иконка приложения: assets/icon.ico (если файла нет — строка игнорируется).
_icon_file = project_dir / "assets" / "icon.ico"
icon_path = str(_icon_file) if _icon_file.is_file() else None


a = Analysis(  # noqa: F821 — имена PyInstaller подставляет сам
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    # Данные в поставку не включаются: запрет скачивается отдельно.
    datas=[],
    hiddenimports=[
        "PyQt6.QtCharts",
        "PyQt6.QtSvg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Тяжёлые и ненужные пакеты: уменьшают размер exe.
        "tkinter",
        "unittest",
        "pydoc",
    ],
    noarchive=False,
    optimize=0,
)


pyz = PYZ(a.pure)  # noqa: F821


exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ZapretGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Иконка: assets/icon.ico, если файл есть (иначе None — иконка PyInstaller).
    icon=icon_path,
    # Файл версии Windows (VERSIONINFO) не используется: его в проекте нет.
    version=None,
)
