"""Сторож изоляции проверок: файл тестов обязан проходить в одиночку (T098).

Почему это отдельная проверка, а не «и так понятно».

Прогон отдельного файла — обычный способ работать над одной областью. Если файл
зеленеет только в компании соседей, человек видит красное на исправном коде и
чинит то, что не сломано. Обратный случай тише и хуже: проверка, зависящая от
порядка сбора, однажды перестанет что-то проверять, и никто этого не заметит.

Что именно держит эта проверка (issue #79). Django настраивался **побочным
эффектом** фикстуры `web_env`: пока веб-тесты собирались раньше, «чистые» тесты
без базы получали настроенные переводы даром. Запуск такого файла в одиночку
падал шестью проверками с `ImproperlyConfigured: Requested setting USE_I18N`,
хотя проверяемый код был исправен.

Проверок здесь две, и они разного веса:

- **инвариант** — импорт `conftest` настраивает Django сам, без фикстур. Это
  причина, и её дешевле сторожить прямо;
- **приёмка** — те самые файлы, которые падали, запускаются в отдельном
  процессе поодиночке. Это дороже (два подпроцесса), но проверяет ровно то, на
  что жаловался человек, а не наше представление об этом.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

# Файлы, которые считают деньги без базы и потому зовут gettext на «чистых»
# данных. Именно они падали в одиночку. Список короткий намеренно: подпроцесс
# стоит секунд, и сторожить им весь каталог значило бы удвоить прогон.
ALONE = [
    "tests/test_reports_reconcile.py",
    "tests/test_reports_variance.py::test_a_preset_without_thresholds_refuses_instead_of_inventing_them",
]


def _clean_env() -> dict:
    """Окружение без следов текущего прогона.

    `DATABASE_URL` и настройки Django выставляет сам `conftest`; передавать в
    подпроцесс те, что уже стоят здесь, значило бы проверять не одиночный
    запуск, а запуск с подсказкой.
    """
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL"}}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    # Покрытие подпроцессов здесь не нужно и мешает: подпроцесс сам запускает
    # pytest, и хук покрытия удваивал бы файлы отчёта.
    env.pop("COVERAGE_PROCESS_START", None)
    return env


def test_importing_conftest_configures_django():
    """Переводы работают сразу после импорта `conftest`, без единой фикстуры.

    Это причина issue #79 одной строкой: настройка Django не должна быть
    побочным эффектом фикстуры, которую чистый тест не просит.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(TESTS)!r}); "
        "import conftest; "
        "from django.utils.translation import gettext; "
        "print(gettext('Файл не выбран.'))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=_clean_env(), capture_output=True, text=True,
    )
    assert done.returncode == 0, (
        "импорт conftest не настраивает Django — чистые тесты снова зависят "
        f"от порядка сбора:\n{done.stderr[-2000:]}"
    )


@pytest.mark.parametrize("target", ALONE)
def test_a_pure_file_passes_alone(target):
    """Тот же файл, запущенный один, зелёный — как у человека за клавиатурой."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, env=_clean_env(), capture_output=True, text=True,
    )
    assert done.returncode == 0, (
        f"{target} в одиночку красный:\n{done.stdout[-3000:]}\n{done.stderr[-2000:]}"
    )
