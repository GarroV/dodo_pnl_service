"""README обещает число тестов — оно обязано быть настоящим (T122).

README писал «655 зелёных тестов и 8 пропущенных», когда прогон давал больше
тысячи: число отстало в полтора раза и никем не проверялось. Само по себе это
мелочь, но читает README тот, кто поднимает продукт впервые, и расхождение в
полтора раза — первое, что он о нём узнаёт: значит, инструкции устарели, значит,
им нельзя верить.

Число тут же и устареет снова, если его никто не сторожит. Поэтому проверка
сравнивает обещание с фактом — количеством собранных проверок, — и краснеет
ровно тогда, когда кто-то добавил или убрал тест и забыл поправить README.

Собранных, а не пройденных: полный прогон внутри прогона стоил бы вдвое дороже
всего остального, а сбор занимает доли секунды. Числа сходятся, пока прогон
зелёный: собранное = зелёные + пропущенные.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

PROMISE = re.compile(
    r"Ожидаемый результат — (\d+) зелёных тестов и (\d+) пропущенных"
)


def collected() -> int:
    """Сколько проверок в репозитории сейчас — тем же pytest, что и у человека."""
    done = subprocess.run(
        # Без своего `-q`: на `-qq` (а он получался, пока `-q` стоял ещё и в
        # `addopts`) итоговой строки со счётом нет вовсе. Из `addopts` флаг с
        # тех пор убран (issue #93), но добавлять его здесь всё равно незачем —
        # считается строка, которую печатает обычный прогон.
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=ROOT, capture_output=True, text=True,
    )
    found = re.search(r"(\d+) tests collected", done.stdout + done.stderr)
    assert found, f"pytest не сказал, сколько тестов собрал:\n{done.stdout[-2000:]}"
    return int(found.group(1))


def test_the_readme_promises_the_number_of_tests_that_exist():
    """Обещание README сходится с тем, что в репозитории на самом деле.

    Красный здесь — не поломка продукта: скорее всего вы добавили или убрали
    тест. Поправьте одну строку в README (число и дату прогона) — и всё.
    """
    promise = PROMISE.search(README.read_text(encoding="utf-8"))
    assert promise, "в README больше нет обещания про число тестов — или оно переписано"

    green, skipped = int(promise.group(1)), int(promise.group(2))
    assert green + skipped == collected(), (
        f"README обещает {green} зелёных и {skipped} пропущенных "
        f"(вместе {green + skipped}), а тестов в репозитории {collected()}. "
        "Поправьте число и дату прогона в README."
    )
