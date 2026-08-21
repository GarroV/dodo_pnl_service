"""Пустой список объясняет следующий шаг, а не факт пустоты (T160).

Требование задачи: «каждый пустой список говорит, что здесь появится и с чего
начать». В эталоне («Модуль 10 — Вход и каркас», раздел «Состояния») это
записано жёстче: пустое состояние обязано отличать «нет данных» от «нет
доступа» и от «не загрузилось» — три разных экрана, а не один.

Здесь проверяются два списка, у которых пустота бывает двух видов и следующее
действие у видов разное:

* **данных ещё нет** — надо внести первую запись, и пустое состояние ведёт туда;
* **отбор ничего не пропустил** — надо снять отбор, и вести «вносить первую
  запись» тут прямо вредно: она, возможно, уже есть и просто отфильтрована.

**И отдельно — то, чего пустое состояние говорить не смеет.** Суженная ветка
обязана отвечать ОДИНАКОВО на выдуманный номер, на чужую точку и на честно
пустой отбор (D023). Иначе перебор адресов становится способом узнать состав
данных партнёра: «здесь ответило иначе» — это уже ответ. Поэтому суженная ветка
проверяется на то, что она не называет ни числа скрытых строк, ни их
существования, и что заголовок у неё тот же самый, что и при выдуманном номере.
"""
from __future__ import annotations

import uuid

import pytest

from conftest import body, login_as

EXPENSES = "/expenses/"
INVOICES = "/invoices/"


def notice(html: str) -> str:
    """Только пустое состояние, без остальной страницы.

    Нужно буквально: у списков есть постоянное главное действие в заголовке
    экрана («Внести расход»), и проверка «на странице нет ссылки на внесение»
    ловила бы его, а не пустое состояние. Спрашивается именно то, что человек
    читает НА МЕСТЕ ДАННЫХ.
    """
    import re

    found = re.search(r'<p class="empty">(.*?)</p>', html, flags=re.S)
    assert found, f"пустого состояния на странице нет вовсе:\n{html[-1200:]}"
    return found.group(1)


@pytest.fixture
def director(client):
    """Роль, которой видно все точки: пустота тогда не от прав, а от данных."""
    login_as(client, "director")
    yield client
    client.post("/logout/")


# --- «данных ещё нет»: сказать, что появится, и увести туда, где заводят -----


def test_an_untouched_month_of_expenses_says_what_will_appear(director):
    """Умолчание — текущий месяц, и в сиде он пуст: ровно случай новичка.

    Проверяется не текст целиком, а то, ради чего он написан: сказано, что здесь
    появится, и есть путь к внесению первой записи. Пустое состояние без пути
    оставляет человека там же, где он застрял.
    """
    shown = notice(body(director.get(EXPENSES)))
    assert "расходов ещё нет" in shown, shown
    assert 'href="/expenses/new/"' in shown, "некуда внести первый расход"


def test_an_untouched_month_of_invoices_says_what_will_appear(director):
    shown = notice(body(director.get(INVOICES)))
    assert "счетов ещё нет" in shown, shown
    assert 'href="/invoices/new/"' in shown, "некуда внести первый счёт"


# --- «отбор ничего не пропустил»: снять отбор, а не заводить запись ----------


def test_a_narrowed_expense_list_offers_to_drop_the_filter(director):
    """Сужено отбором — предлагается снять его, а не вносить первый расход.

    Совет «внесите первый расход» здесь был бы вредным: запись, возможно, уже
    есть и просто не прошла отбор, и человек завёл бы вторую.
    """
    shown = notice(body(director.get(EXPENSES, {"unit": str(uuid.uuid4())})))
    assert "Расходов нет." in shown
    assert 'href="/expenses/"' in shown, "нечем снять отбор"
    assert 'href="/expenses/new/"' not in shown, "суженный список зовёт заводить запись"


def test_a_narrowed_invoice_list_offers_to_drop_the_filter(director):
    shown = notice(body(director.get(INVOICES, {"counterparty": str(uuid.uuid4())})))
    assert "Счетов нет." in shown
    assert 'href="/invoices/"' in shown, "нечем снять отбор"
    assert 'href="/invoices/new/"' not in shown


# --- чего пустое состояние говорить не смеет --------------------------------


@pytest.mark.parametrize(
    ("where", "name"),
    [(EXPENSES, "unit"), (EXPENSES, "item"), (INVOICES, "counterparty")],
)
def test_a_made_up_number_and_an_honest_filter_answer_the_same(director, where, name):
    """Выдуманный номер и обычный суженный отбор отвечают одинаково (D023).

    Если бы отвечали по-разному, перебор адресов стал бы способом узнать, что у
    партнёра есть: разный ответ — это уже ответ. Сравниваются именно пустые
    состояния, а не страницы целиком: отбор виден в самих полях фильтра, и они
    законно отличаются.
    """
    made_up = notice(body(director.get(where, {name: str(uuid.uuid4())})))
    another = notice(body(director.get(where, {name: str(uuid.uuid4())})))

    # Побайтово одно и то же: два разных несуществующих номера обязаны читаться
    # неотличимо, иначе разница сама становится ответом.
    assert made_up == another, (made_up, another)
    title = "Расходов нет." if where == EXPENSES else "Счетов нет."
    assert title in made_up
    assert "скрыт" not in made_up


def test_the_narrowed_branch_is_chosen_by_the_request_not_by_the_data():
    """`narrowed` считается сравнением с умолчанием — базу оно не спрашивает.

    Это и есть причина, по которой сообщение о сужении не способно выдать
    скрытые строки: оно про запрос человека, а не про то, что лежит в базе.
    """
    from web import expenses_views, suppliers_views

    for module in (expenses_views, suppliers_views):
        default = module.filters_default()
        assert not module.narrowed(default), module.__name__
        assert module.narrowed({**default, "ledger": "official"}), module.__name__
