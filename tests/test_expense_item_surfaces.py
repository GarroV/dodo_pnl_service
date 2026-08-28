"""Где статья расходов предлагается: «где выбирается» (T191, модуль 14 эталона).

Мысль модуля одна: **список длиной в сорок строк никто не читает**. Управляющий
на телефоне выбирает из шести статей с пометкой «расходы наличными», а полный
список нужен бухгалтеру и операционному директору.

Проверяется поэтому не «поле сохранилось», а три разных утверждения, и каждое
ловит свой способ сломать задачу:

1. **Форма расхода наличными предлагает не всё.** Иначе поле есть, а толку нет.
2. **Форма разнесения накладной предлагает своё**, и оно другое. Иначе поле есть
   у одной формы и забыто у второй, а по первой это не видно.
3. **Справочник остаётся один.** Поверхность — не право видеть: спрятать статью
   от справочника значило бы завести второй справочник, что и запрещено задачей.

Отдельно — **статья уже выбранного расхода остаётся в списке правки**, даже если
её с этой поверхности потом сняли. Без этого правка комментария у старого
расхода молча меняла бы ему статью: браузер отправляет то, что показано, а
показанного значения там больше нет.
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from conftest import body, login_as
from core.models import BANK, CASH, INVOICE, ExpenseItem

pytestmark = pytest.mark.usefixtures("web_env")


@pytest.fixture
def sql(web_env):
    """Прямое соединение к базе стенда — только чтобы готовить и убирать данные."""
    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def items(sql):
    """Две статьи с разными поверхностями и уборка за собой.

    Названия разные и узнаваемые: искать их в разметке по коду нельзя — код в
    списке выбора не показывается, а совпадение по случайной подстроке дало бы
    зелёный тест при любом поведении.
    """
    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    tenant = sql.execute("select id from tenants order by code limit 1").fetchone()[0]
    made = []
    for code, titles, surfaces in (
        ("t191-cash", "Вода на точку", [CASH]),
        ("t191-invoice", "Канцелярия по накладной", [INVOICE]),
    ):
        made.append(sql.execute(
            """insert into expense_items
                   (tenant_id, code, titles, pnl_item_id, valid_from, surfaces)
               values (%s, %s, %s, %s, '2026-01-01', %s) returning id""",
            [tenant, code, Jsonb({"ru": titles, "en": titles, "sr-latn": titles}),
             line, surfaces],
        ).fetchone()[0])
    try:
        yield {"cash": made[0], "invoice": made[1], "tenant": tenant, "line": line}
    finally:
        sql.execute("delete from expense_items where code like 't191-%'")


# --- база: что нельзя записать вовсе ------------------------------------------


def test_an_item_offered_nowhere_is_refused_by_the_database(sql, items):
    """Статья без единой поверхности не пишется.

    Не «форма не даст»: писать в справочник будут и загрузка файла бухгалтера,
    и будущий API. Статья, не предлагаемая нигде, — не закрытая статья (у той
    есть `valid_to`, и она остаётся в прошлых записях), а строка, которую нельзя
    выбрать ни в одной форме продукта.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        sql.execute(
            "update expense_items set surfaces = '{}' where id = %s", [items["cash"]]
        )


def test_an_unknown_surface_is_refused_by_the_database(sql, items):
    """Выдуманная поверхность не пишется: список поверхностей задаёт код."""
    with pytest.raises(psycopg.errors.CheckViolation):
        sql.execute(
            "update expense_items set surfaces = '{telepathy}' where id = %s",
            [items["cash"]],
        )


def test_items_made_before_the_task_are_offered_everywhere(sql, items):
    """Умолчание — все три поверхности, а не «нигде».

    Статья, заведённая без ответа на новый вопрос, обязана вести себя ровно как
    до задачи. Обратное умолчание вычистило бы списки выбора у всех сразу и
    молча: форма показала бы пустой список, а причина лежала бы в колонке, о
    которой никто не знает.
    """
    made = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 't191-default', %s, %s, '2026-01-01') returning surfaces""",
        [items["tenant"], Jsonb({"ru": "Без ответа"}), items["line"]],
    ).fetchone()[0]
    assert sorted(made) == sorted([BANK, CASH, INVOICE])


# --- формы: кто что предлагает -------------------------------------------------


def test_the_cash_form_offers_only_cash_items(client, items):
    """Расход наличными предлагает статью «расходы наличными» и не предлагает чужую."""
    login_as(client, "admin")
    page = body(client.get("/expenses/new/"))
    assert "Вода на точку" in page
    assert "Канцелярия по накладной" not in page


def test_the_invoice_form_offers_only_invoice_items(client, items):
    """Разнесение накладной предлагает своё — и это другой список.

    Проверка парная к предыдущей нарочно: поле, добавленное одной форме и
    забытое второй, по первой проверке неотличимо от сделанной задачи.
    """
    login_as(client, "admin")
    page = body(client.get("/invoices/new/"))
    assert "Канцелярия по накладной" in page
    assert "Вода на точку" not in page


def test_the_directory_still_shows_every_item(client, items):
    """Справочник остаётся один: поверхность — не право видеть статью.

    Спрятать статью от справочника значило бы завести второй справочник — ровно
    то, что задача запрещает.
    """
    login_as(client, "admin")
    page = body(client.get("/directory/expense-items/"))
    assert "Вода на точку" in page
    assert "Канцелярия по накладной" in page


def test_the_register_filter_still_offers_every_item(client, items):
    """Отбор в реестре расходов перечисляет все статьи.

    Реестр — экран бухгалтера, и ему нужен полный список: расход по статье,
    снятой с наличных, иначе стало бы нечем найти.
    """
    login_as(client, "admin")
    page = body(client.get("/expenses/"))
    assert "Вода на точку" in page
    assert "Канцелярия по накладной" in page


# --- правка старого расхода ----------------------------------------------------


def test_editing_keeps_an_item_that_is_no_longer_offered(client, sql, items):
    """Статья уже выбранного расхода остаётся в списке правки.

    Сняли статью с наличных — расходы, уже на неё записанные, никуда не делись.
    Если список правки её не покажет, браузер отправит первое попавшееся
    значение, и правка комментария молча переназначит статью, то есть строку
    P&L. Молча — потому что человек не трогал это поле вовсе.
    """
    login_as(client, "admin")
    unit = sql.execute(
        "select id from units where tenant_id = %s order by code limit 1",
        [items["tenant"]],
    ).fetchone()[0]
    saved = client.post("/expenses/new/", {
        "date": "2026-06-10", "amount": "1200", "item": str(items["cash"]),
        "unit": str(unit), "note": "T191 правка", "entry_key": "",
    })
    assert saved.status_code == 302, body(saved)

    fact = sql.execute(
        """select id from facts
            where note = 'T191 правка' and superseded_at is null
            order by created_at desc limit 1"""
    ).fetchone()
    assert fact is not None, "расход не записался — проверять правку нечего"

    sql.execute(
        "update expense_items set surfaces = %s where id = %s",
        [[INVOICE], items["cash"]],
    )
    try:
        page = body(client.get(f"/expenses/{fact[0]}/"))
        assert "Вода на точку" in page, (
            "статья расхода пропала из формы правки — правка переназначит её молча"
        )
    finally:
        sql.execute("delete from facts where note = 'T191 правка'")


# --- экран справочника ---------------------------------------------------------


def test_the_card_saves_the_chosen_surfaces(client, sql, items):
    """Выбор в карточке доезжает до базы, а не остаётся картинкой."""
    login_as(client, "admin")
    line = str(items["line"])
    answer = client.post(f"/directory/expense-items/{items['cash']}/", {
        "code": "t191-cash", "title_ru": "Вода на точку", "title_en": "Water",
        "title_sr_latn": "Voda", "pnl_item": line,
        "valid_from": "2026-01-01", "valid_to": "",
        "surfaces": [CASH, BANK],
    })
    assert answer.status_code == 302, body(answer)
    item = ExpenseItem.objects.get(pk=items["cash"])
    assert sorted(item.surfaces) == sorted([BANK, CASH])


def test_the_card_refuses_an_item_offered_nowhere(client, items):
    """Ни одной галки — отказ словами, а не запись, которую нигде не выбрать."""
    login_as(client, "admin")
    answer = client.post(f"/directory/expense-items/{items['cash']}/", {
        "code": "t191-cash", "title_ru": "Вода на точку", "title_en": "Water",
        "title_sr_latn": "Voda", "pnl_item": str(items["line"]),
        "valid_from": "2026-01-01", "valid_to": "",
    })
    assert answer.status_code == 400
    assert "Где выбирается" in body(answer)


def test_the_list_names_where_each_item_is_offered(client, items):
    """В списке видно, где статья предлагается: иначе поле правится вслепую."""
    login_as(client, "admin")
    page = body(client.get("/directory/expense-items/"))
    assert "Где выбирается" in page
    assert "Наличные расходы" in page
    assert "Разнесение накладных" in page
