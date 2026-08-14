"""Отказ формы справочника виден не только человеку, но и коду ответа (T142).

Что было. `BadInput` несёт `http_status = 400`, но пять форм из шести его не
читали: `except BadInput as bad: error = bad.message` — и страница уходила с
умолчанием 200. Человек при этом видел объяснение, а всё, что смотрит на код
ответа, а не на разметку — смоук, журнал сервера, будущий вызов по HTTP, бот, —
считало запись удавшейся (issue #112).

Почему это проверяется одним тестом на все формы сразу. Ровно так же был устроен
issue #109: одна форма починена, остальные пять остались, и проверка на одном
экране зеленела бы при починке «по одному». Отказ базы по ограничению уже
отвечает 400 на всех шести (T136), и разные коды на двух причинах отказа одной и
той же формы — это две правды об одном событии.

Граница проверки. Здесь только **разбор ввода**: пустое обязательное поле,
непонятная дата, месяц не месяцем. Отказ по состоянию данных (закрытый месяц)
отвечает 409 и остаётся 409 — это другое событие и другой ответ, и сводить их к
одному числу было бы потерей, а не порядком.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as
from test_directory import sql  # noqa: F401
from test_expense_items_screen import expense_line, items_removed  # noqa: F401
from test_expense_items_screen import form as item_form


def _employee_id(sql) -> str:  # noqa: F811
    return str(
        sql.execute(
            "select e.id from employees e join tenants t on t.id = e.tenant_id "
            "where t.code = 'rs-dev' order by e.last_name limit 1"
        ).fetchone()[0]
    )


def _bad_expense_item(sql):  # noqa: F811
    """Статья расходов без кода. Эта форма отвечала 400 и до задачи — эталон."""
    return "/directory/expense-items/new/", item_form("", pnl_item=expense_line(sql))


def _bad_unit(sql):  # noqa: F811
    """Случай из issue #112 дословно: дата, которая не дата."""
    return "/directory/units/new/", {
        "code": "ZZ-bad", "title": "Точка с кривой датой", "legal_entity": "",
        "opened_at": "первое января", "closed_at": "",
    }


def _bad_legal_entity(sql):  # noqa: F811
    return "/directory/legal-entities/new/", {"title": "", "tax_number": "123"}


def _bad_group(sql):  # noqa: F811
    scheme, ledger = sql.execute(
        "select scheme, ledger from employee_groups order by code limit 1"
    ).fetchone()
    return "/directory/groups/new/", {
        "code": "", "title": "Группа без кода", "scheme": scheme, "ledger": ledger,
    }


def _bad_employee(sql):  # noqa: F811
    return f"/directory/employees/{_employee_id(sql)}/", {
        "what": "person", "last_name": "", "first_name": "Петар",
        "external_id": "0101990800123", "hired_at": "2026-01-01", "dismissed_at": "",
    }


def _bad_calendar_month(sql):  # noqa: F811
    return "/directory/calendar/new/", {
        "month": "июнь", "norm_hours": "168", "working_days": "21",
    }


FORMS = {
    "статьи расходов": _bad_expense_item,
    "точки": _bad_unit,
    "юрлица": _bad_legal_entity,
    "группы сотрудников": _bad_group,
    "сотрудники": _bad_employee,
    "календарь": _bad_calendar_month,
}


@pytest.mark.parametrize("screen", list(FORMS))
def test_bad_input_is_refused_with_four_hundred_on_every_directory_form(
    client, sql, items_removed, screen,  # noqa: F811
):
    """Разбор ввода отвечает 400 на каждой форме справочника, а не 200."""
    login_as(client, "admin")
    try:
        url, payload = FORMS[screen](sql)
        answer = client.post(url, payload)

        assert answer.status_code == 400, (
            f"{screen}: ответ {answer.status_code} — «отказано» неотличимо от «сохранено»"
        )
    finally:
        client.post("/logout/")


@pytest.mark.parametrize("screen", list(FORMS))
def test_the_refused_form_still_explains_itself_in_words(
    client, sql, items_removed, screen,  # noqa: F811
):
    """Код ответа не заменяет объяснения: человек по-прежнему читает, что не так.

    Отдельным тестом, а не строкой в предыдущем: 400 без текста — это откат к
    белой странице, ради ухода от которой писалась T136.
    """
    login_as(client, "admin")
    try:
        url, payload = FORMS[screen](sql)
        html = body(client.post(url, payload))

        assert "form" in html, f"{screen}: вместо формы отдана не страница"
        for leak in ("Traceback", "BadInput", "IntegrityError"):
            assert leak not in html, f"{screen}: наружу вылезло «{leak}»"
        # Отказ назвал себя словами. Формулировки у форм разные («обязательно»,
        # «дата пишется как», «месяц пишется как»), общее у них одно — кавычки
        # вокруг названия поля, которое человеку править.
        assert "«" in html or "&quot;" in html or "“" in html, (
            f"{screen}: отказ не назвал поле"
        )
    finally:
        client.post("/logout/")


def test_a_refusal_by_state_still_answers_four_hundred_nine(client, sql):  # noqa: F811
    """Закрытый месяц — не ошибка ввода, и код ответа у него свой.

    Сторож границы: сведя всё к 400, мы потеряли бы единственный признак, по
    которому снаружи видно, что делать дальше — поправить набранное или
    переоткрыть период.
    """
    from web.dbrefusal import BadInput
    from web.directory import DirectoryRefused

    assert BadInput.http_status == 400
    assert DirectoryRefused.http_status == 409
