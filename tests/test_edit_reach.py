"""Правка говорит, на что подействует и на что нет (T139, issues #99 и #100).

Две правки, после которых человек уходил с экрана с неверной картиной. Обе
проходят, обе показывают «сохранено», и обе молчат о своём настоящем охвате —
одна о том, что подействует **позже**, чем человек думает, другая о том, что
подействует **шире**.

**1. Версия правила с датой внутри месяца на этот месяц не влияет вовсе**
(issue #99). Правила выбираются на месяц целиком (`select_rules(tenant, country,
period)`, где `period` — первое число), поэтому `valid_from = 2026-06-15` для
июня не значит ничего: версия начинает действовать с июля. У условий найма всё
иначе — там версия ищется по `valid_from <= конец месяца`, и середина месяца
работает. Одинаковое поле «Действует с» на двух соседних экранах означает
разное, и узнать об этом человеку неоткуда: подсказка под полем обещала, что
«месяцы до даты считаются по-прежнему», а про месяц **с** датой внутри
умалчивала.

**2. Правка производственного календаря задевает всех партнёров страны**
(issue #100). Календарь общий (`SHARED_TABLES`, `0004_rls`), а форма месяца
говорила об этом одной строкой и только при заведении нового месяца — при правке
существующего не говорила ничего. Пока правка закрытого месяца отклонялась,
соседей защищал отказ; после T121 она проходит, и расхождение с закрытым месяцем
появляется у каждого партнёра страны.

Приём тот же, что уже принят для закрытого месяца (`closed_month_warning` /
`closed_month_notice`): сказать **до** правки, что случится, и **после** — что
случилось. Слова живут в одном месте на все экраны, а не копией на каждом.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import body, login_as
from test_web_rules import (  # noqa: F401
    NET_FACTOR,
    overrides_restored,
    payruns_restored,
    sql,
)

INSIDE_JUNE = "2026-06-15"
FIRST_OF_JULY = "2026-07-01"


# =============================================================================
# 1. Правило: с какого месяца версия подействует на самом деле
# =============================================================================


def test_a_date_inside_the_month_takes_effect_only_next_month():
    """Счёт месяца — один и записан один раз, а не выведен на каждом экране."""
    from web import rules

    assert rules.effective_month(date(2026, 6, 1)) == date(2026, 6, 1)
    assert rules.effective_month(date(2026, 6, 15)) == date(2026, 7, 1)
    assert rules.effective_month(date(2026, 12, 31)) == date(2027, 1, 1)


def test_the_rule_form_says_that_rules_are_taken_by_whole_months(client, web_env):
    """До правки: подсказка под датой называет цену середины месяца.

    Прежняя подсказка говорила только про месяцы **до** даты — то есть ровно про
    то, о чём человек не спрашивал, и молчала про месяц, в который он метит.
    """
    login_as(client, "admin")
    html = body(client.get(f"/rules/{NET_FACTOR}/"))
    client.post("/logout/")

    assert "на месяц целиком" in html, "страница не говорит, что правила берутся помесячно"
    assert "первое число" in html, "страница не советует выполнимого"


def test_a_version_dated_inside_the_month_says_from_which_month_it_works(
    client, web_env, overrides_restored,  # noqa: F811
):
    """После правки: названы оба месяца — который не поедет и с которого поедет.

    Без этого «ничего не изменилось» выглядит как «правка не сработала вовсе», и
    человек идёт заводить её второй раз.
    """
    login_as(client, "admin")
    answer = client.post(f"/rules/{NET_FACTOR}/", {"value": "0.65", "valid_from": INSIDE_JUNE})
    assert answer.status_code == 302, body(answer)
    html = body(client.get(answer["Location"]))
    client.post("/logout/")

    assert "Июль 2026" in html, "не сказано, с какого месяца версия подействует"
    assert "Июнь 2026" in html, "не сказано, какой месяц останется прежним"
    assert "2026-06-01" in html, "не сказано, какую дату ставить, чтобы подействовало на июнь"


def test_a_version_dated_on_the_first_says_nothing_extra(
    client, web_env, overrides_restored,  # noqa: F811
):
    """Предупреждение не по делу обесценивает предупреждение по делу.

    Дата первого числа работает ровно так, как человек и ожидает, — говорить тут
    не о чем.
    """
    login_as(client, "admin")
    answer = client.post(f"/rules/{NET_FACTOR}/", {"value": "0.66", "valid_from": FIRST_OF_JULY})
    assert answer.status_code == 302, body(answer)
    html = body(client.get(answer["Location"]))
    client.post("/logout/")

    assert "на месяц целиком" not in html, "страница предупреждает о том, чего не случилось"


def test_a_broken_date_in_the_address_does_not_produce_words(client, web_env):
    """Признак приезжает адресом — значит подставить его может кто угодно.

    Мусор в нём не должен ни ломать страницу, ни рождать фразу о месяце, которого
    не было.
    """
    login_as(client, "admin")
    answer = client.get("/rules/?from=не-дата")
    client.post("/logout/")

    assert answer.status_code == 200
    assert "на месяц целиком" not in body(answer)


# =============================================================================
# 2. Календарь: правка общая для страны
# =============================================================================


@pytest.fixture
def calendar_restored(sql):  # noqa: F811
    """Июньский календарь возвращается: он общий, и от него зависят суммы."""
    was = sql.execute(
        "select norm_hours, working_days from calendars "
        "where country_code = 'RS' and period = '2026-06-01'"
    ).fetchone()
    yield
    sql.execute(
        "update calendars set norm_hours = %s, working_days = %s "
        "where country_code = 'RS' and period = '2026-06-01'",
        was,
    )


def test_the_calendar_form_of_an_existing_month_says_whom_the_edit_reaches(
    client, web_env,
):
    """До правки, и именно на правке существующего месяца.

    Строка про общий календарь была только на форме заведения нового месяца:
    тот, кто правит норму часов уже заведённого, не читал о соседях ничего.
    """
    login_as(client, "admin")
    html = body(client.get("/directory/calendar/2026-06/"))
    client.post("/logout/")

    assert "всех партнёров" in html, "форма месяца молчит о том, что календарь общий"


def test_the_new_month_form_says_the_same_thing(client, web_env):
    """Те же слова, а не вторая формулировка того же самого."""
    login_as(client, "admin")
    html = body(client.get("/directory/calendar/new/"))
    client.post("/logout/")

    assert "всех партнёров" in html


def test_after_a_calendar_edit_the_page_says_whom_it_touched(
    client, web_env, calendar_restored,
):
    """После правки: сказано, что подействовало не только на этого партнёра."""
    login_as(client, "admin")
    answer = client.post("/directory/calendar/2026-06/", {
        "norm_hours": "170", "working_days": "21",
    })
    assert answer.status_code == 302, body(answer)
    html = body(client.get(answer["Location"]))
    client.post("/logout/")

    assert "всех партнёров" in html, (
        "страница календаря молчит о том, что правка задела соседей по стране"
    )


def test_the_calendar_page_is_silent_when_nothing_was_edited(client, web_env):
    """Без правки — без слов о ней: иначе фраза перестанет читаться."""
    login_as(client, "admin")
    html = body(client.get("/directory/calendar/"))
    client.post("/logout/")

    assert "всех партнёров" not in html
