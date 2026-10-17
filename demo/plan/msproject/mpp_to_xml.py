# -*- coding: utf-8 -*-
"""Разовая конверсия плана MS Project: .mpp → MSPDI XML, который читает ABOP.

Зачем отдельный шаг. Формат .mpp — двоичный, и единственный надёжный его разборщик (MPXJ) написан
на Java. Ставить JVM на сервер ради одного формата незачем: ABOP читает XML, который MS Project
сохраняет штатно («Файл → Сохранить как → XML»). Этот скрипт нужен, только когда на руках уже
выгруженный .mpp, а самого MS Project под рукой нет.

    pip install mpxj JPype1          # нужен установленный JDK/JRE 17+
    python demo/plan/msproject/mpp_to_xml.py "План проекта.mpp" plan.xml

Грабля Windows: JVM не читает jar-ы по пути с кириллицей — скрипт копирует их во временный каталог
с латинским путём и стартует JVM уже оттуда.
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile


def конвертировать(исходный: str, целевой: str) -> str:
    try:
        import jpype
        import mpxj
    except ImportError as ex:  # noqa: BLE001 — объясняем, а не падаем трассировкой
        raise SystemExit("нужны пакеты mpxj и JPype1 (и установленная Java): "
                         "pip install mpxj JPype1 — %s" % ex)

    библиотеки = os.path.join(os.path.dirname(mpxj.__file__), "lib")
    временный = os.path.join(tempfile.mkdtemp(prefix="mpxj_"), "lib")
    os.makedirs(временный, exist_ok=True)
    for jar in glob.glob(os.path.join(библиотеки, "*.jar")):
        shutil.copy2(jar, временный)

    jpype.startJVM(classpath=glob.glob(os.path.join(временный, "*.jar")))
    Читатель = jpype.JClass("org.mpxj.reader.UniversalProjectReader")
    Писатель = jpype.JClass("org.mpxj.mspdi.MSPDIWriter")

    проект = Читатель().read(исходный)
    Писатель().write(проект, целевой)
    задач = sum(1 for з in проект.getTasks() if з is not None and з.getName() is not None)
    print("задач: %d → %s" % (задач, целевой))
    return целевой


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("как пользоваться: mpp_to_xml.py <план.mpp> <план.xml>")
    конвертировать(sys.argv[1], sys.argv[2])
