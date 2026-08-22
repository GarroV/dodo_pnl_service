"""Множественные числа русского языка в каталоге перевода (issue #146).

Русский — язык исходника, и обычно ему каталог не нужен: строка написана
по-русски прямо в шаблоне. Но у `{% blocktranslate count %}` шаблон несёт
только ДВЕ формы (единственное/множественное — так задан сам тег), а
русскому нужно ТРИ (1 / 2–4 / 5+). Непереведённый (для русского) msgid уходит
в откат gettext, а тот умеет только «одно/не одно» — то есть подставляет
форму «много» и на 2, и на 5. Отсюда «ждут разбора 2 бумаг» вместо «бумаги».
Лечится тем, что у этих строк заводится перевод даже на языке исходника —
ровно чтобы дать им третью форму.

Числа для проверки взяты не наугад:

* 1, 2, 5 — потому что дефект виден только на 2 (и на 3, 4): на 1 и на 5
  сломанный (двухформенный) вывод СЛУЧАЙНО совпадает с правильным, и проверка
  на паре чисел «1 и 5» была бы зелёной при живом дефекте.
* 21 и 11 — второе место, где дефект виден глазами, и другой механизм: 21
  берёт форму 0 (как 1), 11 — форму 2 (как 5), хотя оба двузначные. Это
  проверяет, что применилось настоящее русское правило `plural=(...)`, а не
  бинарный откат gettext, который единственное число даёт только для n == 1.

Тесты рендерят строку через шаблонный движок Django (движок → тег
`blocktranslate` → gettext → каталог → `.mo`), а не читают текст `.po`
напрямую: проверка по файлу каталога зеленела бы и при несобранном `.mo`,
то есть при том самом отказе, который экран показывает пользователю.
"""
from __future__ import annotations

from pathlib import Path

from django.template import Context, Template
from django.utils import translation

ROOT = Path(__file__).resolve().parent.parent
RU_CATALOG = ROOT / "src" / "locale" / "ru" / "LC_MESSAGES" / "django.po"


def render_plural(singular: str, plural: str, count: int) -> str:
    """Отрисовать `{% blocktranslate count %}` с русским текстом при locale=ru.

    Тело тега собирается из тех же кусков, что лежат в шаблонах продукта
    (`{{ counter }}` внутри, а не `%(counter)s` — это форма подстановки msgid
    в `.po`, а не то, что пишут в шаблоне).
    """
    template = Template(
        "{% load i18n %}"
        "{% blocktranslate count counter=n %}" + singular +
        "{% plural %}" + plural + "{% endblocktranslate %}"
    )
    with translation.override("ru"):
        return template.render(Context({"n": count}))


# Четыре строки — ровно те, что заведены в src/locale/ru/LC_MESSAGES/django.po.
# Ожидания перечислены явно, а не собраны сверкой с каталогом: тест, сверяющий
# каталог с самим собой, зеленеет всегда и не проверяет ничего.
CASES = {
    "inbox: строка": {
        "singular": "ждёт разбора {{ counter }} строка",
        "plural": "ждут разбора {{ counter }} строк",
        1: "ждёт разбора 1 строка",
        2: "ждут разбора 2 строки",
        5: "ждут разбора 5 строк",
    },
    "inbox/papers: бумага": {
        "singular": "ждёт разбора {{ counter }} бумага",
        "plural": "ждут разбора {{ counter }} бумаг",
        1: "ждёт разбора 1 бумага",
        2: "ждут разбора 2 бумаги",
        5: "ждут разбора 5 бумаг",
    },
    "expenses: итого по строкам": {
        "singular": "Итого по {{ counter }} строке",
        "plural": "Итого по {{ counter }} строкам",
        1: "Итого по 1 строке",
        2: "Итого по 2 строкам",
        5: "Итого по 5 строкам",
    },
    "invoices: итого по счетам": {
        "singular": "Итого по {{ counter }} счёту",
        "plural": "Итого по {{ counter }} счетам",
        1: "Итого по 1 счёту",
        2: "Итого по 2 счетам",
        5: "Итого по 5 счетам",
    },
}


def test_ru_plural_forms_on_one_two_and_five():
    """На 2 (и только на 2–4) сломанный откат подставлял форму «много».

    Число 1 и число 5 у двухформенного отката выглядели бы верно и без починки
    — поэтому обе формы 1 и 5 в проверке присутствуют не для полноты, а как
    контроль того, что починка не задела то, что и так работало.
    """
    problems = []
    for label, case in CASES.items():
        for count in (1, 2, 5):
            actual = render_plural(case["singular"], case["plural"], count)
            expected = case[count]
            if actual != expected:
                problems.append(f"{label} @ {count}: {actual!r} != {expected!r}")
    assert not problems, "неверная форма числа:\n" + "\n".join(problems)


def test_ru_plural_forms_on_twenty_one_and_eleven():
    """21 и 11 — оба двузначные, но берут разные формы: это проверяет правило.

    Бинарный откат gettext знает только n == 1 → единственное. Число 21 не
    равно 1, поэтому откат на нём молча подставил бы форму «много» — то есть
    «21 бумаг», ровно так, как жаловались на живом экране (issue #146). Верное
    русское правило `n % 10 == 1 && n % 100 != 11` даёт 21 форму 0, а 11 —
    форму 2 (у 11 тот же остаток от деления на 10, что и у 1, но исключение
    «кроме …11» рассчитано именно на такие числа).
    """
    case = CASES["inbox/papers: бумага"]
    assert render_plural(case["singular"], case["plural"], 21) == "ждёт разбора 21 бумага"
    assert render_plural(case["singular"], case["plural"], 11) == "ждут разбора 11 бумаг"


# --- заголовок каталога и число форм -----------------------------------------
#
# `tests/test_i18n.py::test_the_catalog_survives_a_strict_compile` тоже ловит
# нехватку форм — через настоящий `msgfmt --check`. Но этот тест пропускается,
# если на машине нет gettext, а `nplurals` каталога проверить стоит и без него:
# ниже — независимый (свой, не переиспользованный) разбор ровно этого одного
# факта, а не замена основного парсера каталога.


def _plural_entries(text: str) -> list[list[str]]:
    """Блоки `msgstr[N]` каждой записи с `msgid_plural`, как строки текста.

    Разбор нарочно проще, чем в test_i18n.py: там парсер декодирует содержимое
    строк для сверки с `.mo`, здесь достаточно посчитать количество форм — и
    сделать это НЕ тем же кодом, что в другом файле, чтобы одна ошибка разбора
    не спрятала дефект от обеих проверок разом.
    """
    entries: list[list[str]] = []
    current: list[str] = []
    saw_plural = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if saw_plural:
                entries.append(current)
            current = []
            saw_plural = False
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid_plural"):
            saw_plural = True
        if line.startswith("msgstr["):
            current.append(line)
    if saw_plural:
        entries.append(current)
    return entries


def test_ru_catalog_declares_three_plural_forms():
    """Заголовок каталога обязан просить три формы, а не две.

    Без этого даже правильно заполненные msgstr[0..2] были бы избыточны для
    gettext: он опирался бы на `nplurals` заголовка, а не на число форм записи.
    """
    header = RU_CATALOG.read_text(encoding="utf-8")
    assert "nplurals=3" in header, (
        "заголовок каталога ru не объявляет три формы множественного числа"
    )


def test_every_plural_entry_in_ru_catalog_has_three_forms():
    """Запись с двумя формами вместо трёх — это форма 2 (5+), молча пропавшая.

    `msgfmt` без `--check` (то, что зовёт `compilemessages`) такую запись
    проглатывает без ошибки — `.mo` соберётся, а число 5+ получит пустую
    строку на экране. Эта проверка — то же самое, чем нашли issue #144 на
    сербском каталоге, только руками для русского и без внешнего `msgfmt`.
    """
    entries = _plural_entries(RU_CATALOG.read_text(encoding="utf-8"))
    assert entries, "в каталоге ru нет ни одной записи с msgid_plural"
    incomplete = [entry for entry in entries if len(entry) != 3]
    assert not incomplete, (
        f"записи с числом форм не равным трём ({len(incomplete)}):\n"
        + "\n".join(", ".join(entry) for entry in incomplete)
    )
