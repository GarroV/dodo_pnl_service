"""Экран справочника статей расходов (T108).

Здесь проверяется интерфейс: кто видит раздел, что показывает список, как
отказывают форме и что происходит с правкой, задевающей утверждённый месяц.
Вторая половина пары — в `test_expense_items.py`: там то же самое держит база,
мимо экрана.

**Почему правка привязки к строке P&L отклоняется у закрытого месяца.** Версий
по датам у привязки нет: она одна на всю историю статьи. Значит любая её правка
задевает и уже утверждённый месяц — не потому, что дату выбрали неудачно, а
потому, что выбирать нечего. Тот же случай, что схема расчёта группы, и тот же
отказ (`refuse_if_unversioned_touches_closed_month`).
"""
from __future__ import annotations

import re

import pytest

from conftest import body, login_as
from core.models import ExpenseItem
from test_directory import approve_june, payruns_restored, sql  # noqa: F401

LIST_URL = "/directory/expense-items/"
NEW_URL = "/directory/expense-items/new/"

# Оперативного директора здесь нет с 28.08.2026: справочники ведёт и он
# тоже (D059, ответ владельца). Осталось две роли, которым право не дано.
ROLES_WITHOUT_RIGHT = ["manager"]


def form(code: str, **extra) -> dict:
    """Заполненная форма статьи. Названия на трёх языках — как требует задача."""
    fields = {
        "code": code,
        "title_ru": f"Вода {code}",
        "title_en": f"Water {code}",
        "title_sr_latn": f"Voda {code}",
        "valid_from": "2026-01-01",
        "valid_to": "",
    }
    fields.update(extra)
    return fields


def expense_line(sql) -> str:  # noqa: F811
    """Любая строка P&L, в которую статье законно ложиться."""
    return str(
        sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    )


def another_line(sql) -> str:  # noqa: F811
    return str(
        sql.execute("select id from pnl_items where code = 'payroll_taxes'").fetchone()[0]
    )


@pytest.fixture
def items_removed(sql):  # noqa: F811
    """Статьи, заведённые тестом, не переживают его.

    База стенда живёт весь прогон и одна на все модули: оставленная статья
    попала бы в списки и в формы соседних тестов, а её название — в проверку
    «на нерусской странице нет кириллицы».
    """
    before = {
        row[0] for row in sql.execute("select id from expense_items").fetchall()
    }
    yield
    sql.execute(
        "delete from expense_items where id <> all(%s::uuid[])", (list(before),)
    )


# --- право на раздел ----------------------------------------------------------


def test_the_section_is_listed_among_the_directories(client):
    """Раздел виден в оглавлении справочников, а не только по прямому адресу."""
    login_as(client, "admin")
    assert LIST_URL in body(client.get("/directory/"))
    client.post("/logout/")


@pytest.mark.parametrize("role", ROLES_WITHOUT_RIGHT)
def test_the_other_roles_are_refused_in_words(client, role):
    """Адрес отвечает отказом с названием действия, а не пустотой и не 404."""
    login_as(client, role)
    for url in (LIST_URL, NEW_URL):
        answer = client.get(url)
        assert answer.status_code == 403, f"{role} {url}: {answer.status_code}"
        assert "Ведение справочников" in body(answer), f"{role} {url}: отказ без действия"
    client.post("/logout/")


# --- заведение и правка -------------------------------------------------------


def test_the_admin_creates_an_item_and_sees_it_in_the_list(client, sql, items_removed):  # noqa: F811
    login_as(client, "admin")
    try:
        answer = client.post(NEW_URL, form("water", pnl_item=expense_line(sql)))
        assert answer.status_code == 302, body(answer)

        html = body(client.get(LIST_URL))
        assert "water" in html
        assert "Вода water" in html, "в списке нет названия на языке страницы"
    finally:
        client.post("/logout/")


def test_the_title_follows_the_page_language(client, sql, items_removed):  # noqa: F811
    """Название статьи показывается на языке страницы, а не на языке ввода.

    Это и есть смысл трёх названий: сербский бухгалтер вносит трату, а
    русскоязычный оперативный директор читает её в том же справочнике.
    """
    login_as(client, "admin")
    try:
        client.post(NEW_URL, form("power", pnl_item=expense_line(sql)))

        client.cookies["django_language"] = "en"
        html = body(client.get(LIST_URL))
        assert "Water power" in html, "английская страница показала не английское название"
        assert "Вода power" not in html
    finally:
        client.cookies.pop("django_language", None)
        client.post("/logout/")


def test_an_item_without_a_title_is_refused(client, sql, items_removed):  # noqa: F811
    """Статья без единого названия — строка, которую человек не выберет глазами."""
    login_as(client, "admin")
    try:
        answer = client.post(NEW_URL, form(
            "nameless", pnl_item=expense_line(sql),
            title_ru="", title_en="", title_sr_latn="",
        ))
        assert answer.status_code == 400, answer.status_code
        assert sql.execute(
            "select count(*) from expense_items where code = 'nameless'"
        ).fetchone()[0] == 0
    finally:
        client.post("/logout/")


def test_an_item_without_a_pnl_line_is_refused(client, sql, items_removed):  # noqa: F811
    """Статья без строки P&L не соберётся в отчёт: она никуда не попадёт."""
    login_as(client, "admin")
    try:
        answer = client.post(NEW_URL, form("orphan", pnl_item=""))
        assert answer.status_code == 400, answer.status_code
    finally:
        client.post("/logout/")


def test_a_subtotal_is_not_offered_as_a_pnl_line(client, sql, items_removed):  # noqa: F811
    """Подытог считается из детей: факты в него писать нельзя (`facts_guard`).

    Предлагать его в списке значило бы обещать выбор, который отвергнет база
    при первом же расходе по этой статье.
    """
    subtotal = str(sql.execute("select id from pnl_items where kind = 'subtotal'").fetchone()[0])
    login_as(client, "admin")
    try:
        assert subtotal not in body(client.get(NEW_URL)), "подытог предложен в форме"
        answer = client.post(NEW_URL, form("into-subtotal", pnl_item=subtotal))
        assert answer.status_code == 400, answer.status_code
    finally:
        client.post("/logout/")


def test_the_item_is_edited_from_the_screen(client, sql, items_removed):  # noqa: F811
    login_as(client, "admin")
    try:
        client.post(NEW_URL, form("rent", pnl_item=expense_line(sql)))
        html = body(client.get(LIST_URL))
        match = re.search(r'href="(/directory/expense-items/[0-9a-f-]+/)"', html)
        assert match, f"в списке нет ссылки на карточку статьи:\n{html}"

        answer = client.post(match.group(1), form(
            "rent", pnl_item=expense_line(sql), title_ru="Аренда",
        ))
        assert answer.status_code == 302, body(answer)
        assert "Аренда" in body(client.get(LIST_URL))
    finally:
        client.post("/logout/")


# --- закрытый месяц -----------------------------------------------------------


def test_a_new_item_may_start_inside_a_closed_month_but_says_so(
    client, sql, web_env, items_removed, payruns_restored,  # noqa: F811
):
    """Дата внутри утверждённого месяца проходит — и продукт говорит, что будет.

    Так было не всегда: до T121 здесь стоял отказ. Правило продукта сменилось не
    у статей расходов, а везде (D020): правку **с датой** продукт принимает, а
    закрытый месяц ею не переписывает. У статьи даты версионируются, поэтому
    она подчиняется общему правилу; отказ остался ровно там, где версий нет
    вовсе, — на привязке к строке P&L (проверка ниже).

    Молчаливое согласие тут было бы хуже отказа: человек выбрал дату внутри
    закрытого месяца и обязан узнать, что месяц ею не переписан.
    """
    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "admin")
    try:
        answer = client.post(NEW_URL, form(
            "late", pnl_item=expense_line(sql), valid_from="2026-06-10",
        ), follow=True)
        assert answer.status_code == 200, answer.status_code
        assert ExpenseItem.objects.filter(code="late").exists(), "статья не завелась"
        html = body(answer)
        assert "2026-06" in html, (
            "продукт промолчал о закрытом месяце — человек решит, что месяц переписан"
        )
    finally:
        client.post("/logout/")


def test_rebinding_the_pnl_line_of_an_existing_item_is_refused(
    client, sql, web_env, items_removed, payruns_restored,  # noqa: F811
):
    """Перепривязка статьи к другой строке P&L переписала бы закрытый отчёт.

    Отказ обязан быть без выдуманной даты: поля даты у привязки нет вовсе, и
    совет «возьмите дату позже» человек выполнить не может (T103).
    """
    login_as(client, "admin")
    client.post(NEW_URL, form("power-bill", pnl_item=expense_line(sql)))
    html = body(client.get(LIST_URL))
    card = re.search(r'href="(/directory/expense-items/[0-9a-f-]+/)"', html).group(1)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "admin")
    try:
        answer = client.post(card, form("power-bill", pnl_item=another_line(sql)))
        assert answer.status_code == 409, answer.status_code
        html = body(answer)
        assert "0001-01-01" not in html, "отказ назвал дату, которой человек не вводил"
        assert "2026-06" in html
    finally:
        client.post("/logout/")


def test_renaming_stays_allowed_when_a_month_is_closed(
    client, sql, web_env, items_removed, payruns_restored,  # noqa: F811
):
    """Название денег не двигает: запрещать его правку значило бы отказывать зря."""
    login_as(client, "admin")
    client.post(NEW_URL, form("water-bill", pnl_item=expense_line(sql)))
    html = body(client.get(LIST_URL))
    card = re.search(r'href="(/directory/expense-items/[0-9a-f-]+/)"', html).group(1)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "admin")
    try:
        answer = client.post(card, form(
            "water-bill", pnl_item=expense_line(sql), title_ru="Вода холодная",
        ))
        assert answer.status_code == 302, body(answer)
        assert "Вода холодная" in body(client.get(LIST_URL))
    finally:
        client.post("/logout/")
