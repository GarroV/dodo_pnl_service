"""Подписи колонок ведомости и табеля на языке страницы (T092).

Тут проверяется то, ради чего задача и заводилась: **колонки главной таблицы
продукта**. Проверка соседняя, но не та же, что `test_rule_titles.py`: там —
что подпись объявлена во всех языках, здесь — что на экран приезжает именно та,
которая объявлена для языка страницы.

Почему обеими сразу. Подписи в ведомость попадают не так, как в табель:

* табель берёт их из правил живьём — значит, чинится свёрткой в слое правил;
* ведомость показывает `pay_components.title` — подпись, **замороженную в момент
  расчёта**. Она одна и языку страницы не подчиняется. Чтобы колонка говорила на
  языке читателя, экран берёт подпись заново по коду компонента.

Дефект жил ровно между ними: правила перевели, а ведомость по-прежнему
показывала хранимое. Поэтому здесь обе таблицы, на всех трёх языках.
"""
from __future__ import annotations

import re

import pytest

from conftest import body, login_as, period_url, wipe_payruns

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

# Языки продукта и подпись одного и того же вида часов на каждом. Взята
# «отработанные»: это первая колонка обеих таблиц и единственная, которая есть
# у каждого сотрудника, — то есть её отсутствие невозможно списать на данные.
EXPECTED = {
    "ru": "Отработанные",
    "en": "Worked hours",
    "sr-latn": "Radni sati",
}


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь: без расчёта у ведомости нет ни одной колонки."""
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    return None


def headers(html: str) -> list[str]:
    """Подписи колонок таблицы — как их видит человек, без разметки внутри."""
    found = []
    for cell in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S):
        text = re.sub(r"<[^>]+>", " ", cell)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            found.append(text)
    return found


def named(shown: list[str], title: str) -> bool:
    """Подпись есть среди колонок. Вхождением, а не равенством: табель дописывает
    к названию процент оплаты («Radni sati 100%»), и это его дело, не наше."""
    return any(title in name for name in shown)


def screen(client, url: str, language: str) -> str:
    client.cookies["django_language"] = language
    return body(client.get(url))


@pytest.mark.parametrize("language", list(EXPECTED))
def test_the_payroll_sheet_names_its_columns_in_the_page_language(
    client, calculated_june, language
):
    """Колонка ведомости названа на языке страницы, а не на языке расчёта.

    Именно это видел директор на сербской странице: обвязка переведена, а
    колонки — «Отработанные», «Праздничные», «Больничный».
    """
    login_as(client, "director")
    shown = headers(screen(client, period_url(client), language))
    assert named(shown, EXPECTED[language]), (
        f"на языке {language} колонки ведомости: {shown}"
    )


@pytest.mark.parametrize("language", ["en", "sr-latn"])
def test_no_column_of_the_payroll_sheet_stays_russian(client, calculated_june, language):
    """Ни одной русской подписи колонки на нерусской странице.

    Проверка по кириллице, а не по списку ожидаемых слов: список пришлось бы
    дописывать на каждый новый компонент, и забытый остался бы незамеченным
    ровно так же, как забытый перевод.
    """
    login_as(client, "director")
    left = [name for name in headers(screen(client, period_url(client), language))
            if CYRILLIC.search(name)]
    assert not left, f"на языке {language} колонки остались русскими: {left}"


@pytest.mark.parametrize("language", list(EXPECTED))
def test_the_timesheet_names_its_columns_in_the_page_language(
    client, web_env, language
):
    """Табель — вторая таблица с теми же подписями, и берёт он их иначе.

    Отдельным тестом, потому что чинится другим механизмом: табель читает
    правила живьём, ведомость — хранимое. Одна проверка на двоих пропустила бы
    половину дефекта.
    """
    login_as(client, "director")
    html = body(client.get(period_url(client)))
    link = re.search(r'href="(/timesheets/[^"]+)"', html)
    assert link, "со страницы периода нет перехода в табель"

    shown = headers(screen(client, link.group(1), language))
    assert named(shown, EXPECTED[language]), f"на языке {language} колонки табеля: {shown}"
    if language != "ru":
        left = [name for name in shown if CYRILLIC.search(name)]
        assert not left, f"на языке {language} колонки табеля остались русскими: {left}"


def test_a_component_the_rules_no_longer_know_keeps_the_words_it_was_paid_with(web_env):
    """Код исчез из правил — подпись берётся хранимая, а не пустая.

    Закрытый месяц объясняет себя словами, действовавшими тогда. Если партнёр
    убрал вид часов, показать пустую колонку или голый код значило бы стереть
    объяснение уже выплаченной суммы.
    """
    from web.labels import titles_of

    label = titles_of({"hour_types": {"regular": {"title": "Отработанные"}}})
    assert label.get("hours.regular") == "Отработанные"
    assert "hours.sick" not in label, (
        "подпись выдумана для кода, которого в правилах нет"
    )
