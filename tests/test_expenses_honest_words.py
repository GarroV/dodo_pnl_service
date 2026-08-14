"""Три места, где продукт говорил не то, что есть (T134).

Все три — про слова, а не про деньги, и все три нашлись при чтении экранов
чужими глазами (`docs/forge/blocks/converge7.md`, находки 5, 6 и 7).

**1. Название статьи на экране нераспределённых не переводилось.** Там бралось
`facts.title` — снимок названия, снятый в момент внесения на языке вносившего, —
и на английской странице стояла «Аренда» ровно там, где соседний экран и
выгрузка звали ту же статью `Rent`. Снимок обязан остаться в данных (закрытый
отчёт должен выглядеть как в день закрытия), но читателю показывают название на
его языке.

**2. Подсказка «Месяц уже закрыт» стояла в форме всегда.** Продукт словами
сообщал состояние периода — и сообщал неверно: на свежем сиде не закрыт ни один
месяц. Английский перевод при этом был условным, то есть два языка одного
продукта говорили про одно и то же разное.

**3. Кривое значение `?unit=` и `?item=` молча игнорировалось.** Ответом был
полный список с итогом по всему. Даты в той же функции отказывают — и рядом с
ними стоит комментарий, предостерегающий ровно от тихого умолчания.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    JUNE_DAY,
    current_period,
    entry_key,
    facts_of,
    facts_removed,
    item,
    june_total,
    payload,
    tenant,
    units,
)
from test_directory import approve_june, payruns_restored, sql  # noqa: F401
from test_expenses_allocation import network_expense, rules_removed  # noqa: F401
from test_expenses_list import LIST, WIDE

NEW = "/expenses/new/"
WAITING = "/expenses/unallocated/"
ENGLISH = "/i18n/setlang/"


def in_english(client):
    """Переключить язык страницы на английский — кнопкой в шапке, как человек."""
    answer = client.post(ENGLISH, {"language": "en", "next": "/expenses/"})
    assert answer.status_code in (200, 302), answer.status_code


# --- 1. название статьи переводится -------------------------------------------


def test_the_waiting_list_calls_the_item_the_way_the_rest_of_the_product_does(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """На английской странице статья зовётся так же, как в списке расходов.

    Заведённая тестами статья называется «Вода» / `Water` / `Voda`. До T134 на
    английском экране нераспределённых стояло русское название, а на соседнем —
    английское.
    """
    login_as(client, "director")
    try:
        network_expense(client, item, amount="700.00")
        in_english(client)

        waiting = body(client.get(WAITING))
        assert "Water" in waiting, f"название статьи не переведено:\n{waiting}"
        assert "Вода" not in waiting, "на английской странице осталось русское название"

        # И это то же название, что на соседнем экране: два экрана про одну
        # статью не должны звать её по-разному.
        assert "Water" in body(client.get(LIST, WIDE))
    finally:
        client.post(ENGLISH, {"language": "ru", "next": "/expenses/"})
        client.post("/logout/")


# --- 2. про закрытый месяц говорится только когда он закрыт --------------------


def test_the_form_does_not_claim_a_closed_month_when_nothing_is_closed(
    client, sql, item, units,  # noqa: F811
):
    """Ни один месяц не закрыт — и продукт про закрытый месяц молчит."""
    login_as(client, "manager")
    try:
        page = body(client.get(NEW))
        assert "закрыт" not in page, f"продукт объявил закрытым месяц, который открыт:\n{page}"
    finally:
        client.post("/logout/")


def test_the_form_names_the_closed_month_and_where_the_expense_lands(
    client, sql, web_env, item, units, facts_removed,  # noqa: F811
    payruns_restored,  # noqa: F811
):
    """Месяц закрыт — сказано, какой именно и куда ляжет расход.

    Оговорка привязана к дате, которая стоит в поле. Форма показывается с
    введённой датой тогда, когда человек уже отправлял её и получил отказ по
    другому полю, — этим путём тест и идёт: сумма «ноль», отказ, форма заново с
    июньской датой при закрытом июне.
    """
    from datetime import date

    from web.i18n import month_title

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "accountant")
    try:
        answer = client.post(NEW, payload(
            item, units, entry_key=entry_key(), date=JUNE_DAY,
            unit=units["NS1"], amount="0",
        ))
        assert answer.status_code == 400, answer.status_code
        page = body(answer)
        assert month_title(date(2026, 6, 1)) in page, page
        assert month_title(current_period()) in page, "не сказано, куда ляжет расход"

        # А с датой открытого месяца той же формы оговорки нет.
        answer = client.post(NEW, payload(
            item, units, entry_key=entry_key(),
            date=current_period().replace(day=5).isoformat(),
            unit=units["NS1"], amount="0",
        ))
        assert "закрыт" not in body(answer), body(answer)
    finally:
        client.post("/logout/")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_both_languages_say_the_same_about_the_closed_month(
    client, sql, item, units, language,  # noqa: F811
):
    """Про незакрытый месяц оба языка молчат одинаково.

    Раньше русская строка утверждала закрытие, а английская была условной, —
    один продукт, два разных утверждения об одном и том же.
    """
    login_as(client, "manager")
    try:
        client.post(ENGLISH, {"language": language, "next": NEW})
        page = body(client.get(NEW))
        assert "закрыт" not in page and "closed" not in page.lower(), page
    finally:
        client.post(ENGLISH, {"language": "ru", "next": NEW})
        client.post("/logout/")


# --- 3. кривой отбор отказывает, а не расширяет --------------------------------


@pytest.mark.parametrize("name", ["unit", "item"])
@pytest.mark.parametrize("where", [LIST, "/api/expenses/"])
def test_a_broken_filter_is_refused_instead_of_answering_with_everything(
    client, sql, item, units, facts_removed, name, where,  # noqa: F811
):
    """`?unit=zzz` и `?item=zzz` отвечают отказом, а не полным списком.

    Отказ одинаков у экрана и у вызова: разбор отбора у них один (T133). Вызов
    заведён ради бота, а бот, спросивший «расходы по статье X», получал расходы
    по всем статьям с итогом по всем — и докладывал это как ответ на свой
    вопрос.
    """
    login_as(client, "manager")
    try:
        response = client.get(where, {**WIDE, name: "zzz"})
        assert response.status_code == 400, response.status_code
        assert "zzz" in body(response), body(response)[:600]
    finally:
        client.post("/logout/")


@pytest.mark.parametrize("name", ["unit", "item"])
def test_a_made_up_number_still_answers_with_emptiness(
    client, sql, item, units, facts_removed, name,  # noqa: F811
):
    """Отказ по кривому значению ничего не рассказывает о существующих строках.

    Номер, которого нет, по-прежнему даёт пустой список, а не отказ (D023):
    отвергается только то, что номером не является вовсе, — а значит и
    существовать не может.
    """
    import uuid as _uuid

    login_as(client, "manager")
    try:
        response = client.get(LIST, {**WIDE, name: str(_uuid.uuid4())})
        assert response.status_code == 200, response.status_code
        assert "Расходов нет" in body(response)
    finally:
        client.post("/logout/")
