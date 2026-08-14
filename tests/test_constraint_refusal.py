"""Ограничение базы, нарушенное вводом человека, — отказ формы, а не 500 (T136).

Что было. Администратор заводил статью расходов с кодом, который уже есть, и
получал белую страницу: `duplicate key value violates unique constraint
"expense_items_tenant_code_uniq"` (issue #109). Соседние проверки той же формы
(пустое поле, неверный выбор, дата в закрытом месяце) отвечали понятным текстом,
выбивался ровно этот случай. Того же класса issue #98: отложенные внешние ключи
Django проверяются **на коммите**, за пределами обработки отказа, и выдуманная
ссылка тоже давала 500 — там это закрыли на одном экране.

Почему проверок несколько и почему они на разных экранах. У каждой таблицы
справочника свои уникальные ключи и свои ссылки, и починка одного экрана
оставляет остальные: код статьи, код точки и код группы — три разных ограничения
в трёх разных формах. Проверка, стоящая на одном экране, зеленела бы и на
починке «по одному», ради которой issue и заведена.

**Громкое падение обязано остаться громким.** Ограничение, нарушенное дефектом
кода (пустое поле, которого форма не спрашивает), не должно превращаться в
вежливое сообщение: тогда дефект уезжает в журнал вместо экрана и живёт там
месяцами. Поэтому переводятся ровно три класса отказа — совпадение уникального
ключа, ссылка в никуда и пересечение периодов, — а всё остальное падает как
падало. Это проверяется отдельно и намеренно.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction

from conftest import body, login_as
from core.models import Unit
from test_directory import sql  # noqa: F401
from test_expense_items_screen import expense_line, items_removed  # noqa: F401
from test_expense_items_screen import form as item_form
from web.dbrefusal import ConstraintRefused, saving

ITEMS_NEW = "/directory/expense-items/new/"
UNITS_NEW = "/directory/units/new/"
GROUPS_NEW = "/directory/groups/new/"

TENANT = "(select id from tenants where code = 'rs-dev')"


def _duplicate_expense_item(client, sql):  # noqa: F811
    """Случай из issue #109: код статьи, который уже есть."""
    code = "water-dup"
    made = client.post(ITEMS_NEW, item_form(code, pnl_item=expense_line(sql)))
    assert made.status_code == 302, body(made)
    return ITEMS_NEW, item_form(code, pnl_item=expense_line(sql))


def _duplicate_unit(client, sql):  # noqa: F811
    code = sql.execute(
        f"select code from units where tenant_id in {TENANT} order by code limit 1"
    ).fetchone()[0]
    return UNITS_NEW, {
        "code": code, "title": "Ещё одна такая же", "legal_entity": "",
        "opened_at": "2026-01-01", "closed_at": "",
    }


def _duplicate_group(client, sql):  # noqa: F811
    code, scheme, ledger = sql.execute(
        f"select code, scheme, ledger from employee_groups where tenant_id in {TENANT} "
        f"order by code limit 1"
    ).fetchone()
    return GROUPS_NEW, {
        "code": code, "title": "Ещё одна такая же", "scheme": scheme, "ledger": ledger,
    }


SCREENS = {
    "статьи расходов": _duplicate_expense_item,
    "точки": _duplicate_unit,
    "группы сотрудников": _duplicate_group,
}


@pytest.mark.parametrize("screen", list(SCREENS))
def test_a_repeated_code_is_refused_in_words_on_every_directory(
    client, sql, items_removed, screen,  # noqa: F811
):
    """Повторный код на любом справочнике: 400 и человеческий текст, а не 500."""
    login_as(client, "admin")
    try:
        url, payload = SCREENS[screen](client, sql)
        answer = client.post(url, payload)

        assert answer.status_code == 400, f"{screen}: ответ {answer.status_code}"
        html = body(answer)
        assert "Код" in html, f"{screen}: отказ не назвал поле"
        assert "уже есть" in html, f"{screen}: отказ не сказал, что случилось:\n{html[:800]}"
        # Ни следа технической страницы: имя ограничения и текст исключения —
        # для журнала, а не для человека.
        for leak in ("Traceback", "IntegrityError", "duplicate key", "_uniq"):
            assert leak not in html, f"{screen}: наружу вылезло «{leak}»"
    finally:
        client.post("/logout/")


def test_the_refused_form_writes_nothing(client, sql, items_removed):  # noqa: F811
    """Отказ не оставляет за собой половину записи: точек не прибавилось."""
    login_as(client, "admin")
    try:
        before = sql.execute(f"select count(*) from units where tenant_id in {TENANT}") \
            .fetchone()[0]
        url, payload = _duplicate_unit(client, sql)
        assert client.post(url, payload).status_code == 400
        after = sql.execute(f"select count(*) from units where tenant_id in {TENANT}") \
            .fetchone()[0]
        assert after == before, "отказ по коду оставил в базе лишнюю точку"
    finally:
        client.post("/logout/")


def test_a_deferred_foreign_key_is_refused_in_words_not_at_commit(web_env, sql):  # noqa: F811
    """Ссылка в никуда отвергается на месте, а не оборванной транзакцией (#98).

    Внешние ключи Django объявляет `deferrable initially deferred`, то есть
    проверяет их **на коммите**. Внешний `atomic` здесь изображает запрос
    (`ATOMIC_REQUESTS`): без `set constraints all immediate` внутри помощника
    отказ пришёл бы уже за его пределами, и проверка упала бы на «не поднято
    ни одного исключения» — ровно тем, чем оборачивался 500 у человека.
    """
    tenant_id = sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
    with transaction.atomic():
        with pytest.raises(ConstraintRefused) as refused:
            with saving():
                Unit(
                    tenant_id=tenant_id, legal_entity_id=uuid4(),
                    code="ZZ-fk-check", title="Точка с выдуманным юрлицом",
                ).save()
        assert "Юрлицо" in refused.value.message, refused.value.message
        assert refused.value.http_status == 400
        transaction.set_rollback(True)


def test_a_constraint_broken_by_a_defect_still_falls_loudly(web_env, sql):  # noqa: F811
    """Пустое обязательное поле — дефект кода, и он обязан падать, а не извиняться.

    Если бы помощник переводил в отказ формы **любой** отказ базы, дефект
    уезжал бы в журнал вместо экрана: продукт отвечал бы «поправьте ввод» там,
    где поправить нечего.
    """
    tenant_id = sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
    with transaction.atomic():
        with pytest.raises(IntegrityError) as broke:
            with saving():
                Unit(tenant_id=tenant_id, code=None, title="Без кода").save()
        assert not isinstance(broke.value, ConstraintRefused), broke.value
        transaction.set_rollback(True)
