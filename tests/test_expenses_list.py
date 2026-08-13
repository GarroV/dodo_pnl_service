"""Список расходов: срез роли, итог, правка и удаление (T110).

Четыре правила, ради которых написан экран, и каждое проверяется отдельно.

**1. Список показывает ровно свой срез каждой роли.** Управляющему — расходы
его точек, бухгалтеру и оперативному директору — все (D036). Проверяется это не
чтением кода представления, а тем, что чужая строка не приходит **ни в таблицу,
ни в итог**: сумма, посчитанная отдельной выборкой мимо политик, показала бы
управляющему деньги, которых он не видит построчно (D023).

**2. Удалённое видно как заменённое.** Расход не исчезает бесследно: строка
помечается заменённой и остаётся в списке с состоянием, а в итог не входит.
Деньги, пропавшие без следа, — худший исход для учёта.

**3. Фильтр, подобранный руками в адресе, не рассказывает о чужом.** Чужая
точка в `?unit=` даёт пустой результат, неотличимый от «такого нет»: ни кода
точки, ни её названия на странице не появляется, и ответ совпадает с ответом на
выдуманный номер (D014, D023).

**4. Итог — сумма показанных строк.** Не отдельная выборка: две выборки одного
и того же расходятся молча, и тогда человек сверяет кассу с числом, которого в
таблице нет.

Проверки идут через экран, тем же путём, что и человек. Прямое чтение базы —
только осмотр результата, и идёт оно владельцем схемы.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

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

LIST = "/expenses/"
NEW = "/expenses/new/"

# Диапазон, в который попадают расходы тестов: с начала июня по конец текущего
# месяца. Явный, а не умолчание экрана: умолчание — «текущий месяц», и июньские
# строки в него не попадают.
WIDE = {"from": "2026-06-01", "to": "2026-12-31"}


def august_day() -> str:
    """День текущего месяца: такой расход ложится в открытый период."""
    return current_period().replace(day=5).isoformat()


def record(client, item_id, units_map, **extra) -> str:
    """Внести расход через форму — тем же путём, что и человек. Вернуть ключ.

    Комментарий подменяется намеренно: у заготовки `payload` в нём стоит код
    точки («вода в BG1»), а здешние проверки как раз о том, что кода чужой точки
    на странице нет. С таким комментарием тест краснел бы от собственных данных.
    """
    key = entry_key()
    extra.setdefault("note", "вода")
    form = payload(item_id, units_map, entry_key=key, date=august_day(), **extra)
    answer = client.post(NEW, form)
    assert answer.status_code == 302, body(answer)
    return key


def shown(page: str) -> list[dict]:
    """Строки таблицы: сумма и состояние, как их отдал экран.

    Сумма читается из машиночитаемого атрибута строки, а не из форматированной
    ячейки: разделители тысяч зависят от языка страницы, и тест на них проверял
    бы формат, а не деньги. Что атрибут не разъехался с показанным числом,
    проверяет `test_the_total_is_the_sum_of_the_shown_rows`.
    """
    return [
        {"amount": Decimal(row.group(1)), "state": row.group(2), "id": row.group(3)}
        for row in re.finditer(
            r'data-amount="([-0-9.]+)" data-state="(\w+)" data-fact="([0-9a-f-]+)"', page
        )
    ]


def total_of(page: str) -> Decimal:
    found = re.search(r'data-total="([-0-9.]+)"', page)
    assert found, f"на странице нет итога:\n{page[:2000]}"
    return Decimal(found.group(1))


def fact_id_of(sql, key: str) -> str:  # noqa: F811
    return str(
        sql.execute(
            "select id from facts where dedup_key = %s and superseded_at is null",
            (f"manual:cash:{key}",),
        ).fetchone()[0]
    )


# --- правило 1: срез роли -----------------------------------------------------


def test_the_manager_sees_only_the_expenses_of_his_own_units(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Чужая точка не приходит ни строкой, ни вкладом в итог.

    Сломайте `unit_visibility` на `facts` — и тест покраснеет дважды: чужая
    строка появится в таблице и сдвинет итог.
    """
    login_as(client, "director")
    mine = record(client, item, units, unit=units["NS1"], amount="100.00")
    alien = record(client, item, units, unit=units["BG1"], amount="500.00")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        page = body(client.get(LIST, WIDE))
        seen = {row["id"] for row in shown(page)}
        assert fact_id_of(sql, mine) in seen, "своя строка не показана"
        assert fact_id_of(sql, alien) not in seen, "показана строка чужой точки"
        assert "BG1" not in page and "Beograd" not in page, "на странице чужая точка"
        assert total_of(page) == Decimal("100.00"), "чужая сумма попала в итог"
    finally:
        client.post("/logout/")


@pytest.mark.parametrize("role", ["accountant", "director"])
def test_the_accountant_and_the_director_see_every_unit(
    client, sql, item, units, facts_removed, role,  # noqa: F811
):
    """Бухгалтер и оперативный директор ведут месяц целиком (D036)."""
    login_as(client, "director")
    mine = record(client, item, units, unit=units["NS1"], amount="100.00")
    other = record(client, item, units, unit=units["BG1"], amount="500.00")
    client.post("/logout/")

    login_as(client, role)
    try:
        page = body(client.get(LIST, WIDE))
        seen = {row["id"] for row in shown(page)}
        assert {fact_id_of(sql, mine), fact_id_of(sql, other)} <= seen
        assert total_of(page) == Decimal("600.00")
    finally:
        client.post("/logout/")


def test_an_invisible_ledger_is_neither_in_the_rows_nor_in_the_total(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Регистр, которого роль не видит, не оставляет следа даже в сумме (D023).

    Управляющий не видит внутреннего регистра (D031). Расход туда вносит
    директор — тем же экраном, — и в списке управляющего этой суммы быть не
    должно ни строкой, ни копейкой в итоге.
    """
    login_as(client, "director")
    hidden = record(
        client, item, units, unit=units["NS1"], amount="333.00", ledger="internal",
    )
    visible = record(client, item, units, unit=units["NS1"], amount="100.00")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        page = body(client.get(LIST, WIDE))
        seen = {row["id"] for row in shown(page)}
        assert fact_id_of(sql, visible) in seen
        assert fact_id_of(sql, hidden) not in seen, "показан расход невидимого регистра"
        assert total_of(page) == Decimal("100.00"), "невидимый регистр попал в итог"
    finally:
        client.post("/logout/")


# --- правило 4: итог — сумма показанных строк ---------------------------------


def test_the_total_is_the_sum_of_the_shown_rows(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Итог считается по тем же строкам, что показаны, и совпадает с их суммой.

    Проверяется и то, что машиночитаемая сумма строки не разъехалась с
    показанной: иначе тест сверял бы итог со скрытым числом, которого человек
    не видит.
    """
    from web.format import money

    login_as(client, "director")
    try:
        record(client, item, units, unit=units["NS1"], amount="100.01")
        record(client, item, units, unit=units["BG1"], amount="200.02")

        page = body(client.get(LIST, WIDE))
        rows = shown(page)
        assert len(rows) >= 2
        assert total_of(page) == sum(row["amount"] for row in rows if row["state"] == "active")
        assert money(total_of(page)) in page, "итог не показан человеку"
        for row in rows:
            assert money(row["amount"]) in page, "показанная сумма не совпала с машинной"
    finally:
        client.post("/logout/")


def test_a_filter_moves_the_rows_and_the_total_together(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Фильтр сужает и таблицу, и итог: они считаются одной выборкой."""
    login_as(client, "director")
    try:
        record(client, item, units, unit=units["NS1"], amount="100.00")
        record(client, item, units, unit=units["BG1"], amount="500.00")

        page = body(client.get(LIST, {**WIDE, "unit": units["NS1"]}))
        rows = shown(page)
        assert total_of(page) == sum(row["amount"] for row in rows if row["state"] == "active")
        assert total_of(page) == Decimal("100.00")
    finally:
        client.post("/logout/")


def test_the_dates_filter_narrows_the_list(
    client, sql, item, units, facts_removed, web_env, payruns_restored,  # noqa: F811
):
    """Дата расхода — та, когда деньги вышли из кассы, по ней и фильтр."""
    login_as(client, "director")
    try:
        june = entry_key()
        assert client.post(
            NEW, payload(item, units, unit=units["NS1"], entry_key=june, date=JUNE_DAY)
        ).status_code == 302
        august = record(client, item, units, unit=units["NS1"], amount="7.00")

        page = body(client.get(LIST, {"from": "2026-06-01", "to": "2026-06-30"}))
        seen = {row["id"] for row in shown(page)}
        assert fact_id_of(sql, june) in seen
        assert fact_id_of(sql, august) not in seen, "фильтр по датам не сузил список"
    finally:
        client.post("/logout/")


# --- правило 3: фильтр не рассказывает о чужом --------------------------------


def test_a_foreign_unit_in_the_filter_is_indistinguishable_from_nothing(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Главная проверка задачи: подобранный в адресе фильтр не выдаёт чужого.

    Ответ на чужую точку обязан совпадать с ответом на выдуманную: иначе
    перебором значений составляется список чужих точек, ни одной из них не
    увидев (D023). Сломайте `unit_visibility` на `facts` — и в таблице появятся
    чужие строки.
    """
    import uuid

    login_as(client, "director")
    record(client, item, units, unit=units["BG1"], amount="500.00")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        alien = body(client.get(LIST, {**WIDE, "unit": units["BG1"]}))
        nobody = body(client.get(LIST, {**WIDE, "unit": str(uuid.uuid4())}))

        assert shown(alien) == [], "чужая строка пришла через фильтр"
        assert total_of(alien) == Decimal("0")
        assert "BG1" not in alien and "Beograd" not in alien
        assert _without_the_filter(alien) == _without_the_filter(nobody), (
            "по ответу видно, что чужая точка существует"
        )
    finally:
        client.post("/logout/")


def _without_the_filter(page: str) -> str:
    """Страница без того, что заведомо различается у двух запросов.

    Убираются три вещи, и ни одна из них не про чужие данные: ключ формы
    (он новый на каждый ответ), адрес возврата у переключателя языка (в нём
    лежит тот самый номер из адреса, который прислал сам клиент) и значения
    полей-uuid. Всё остальное обязано совпасть слово в слово — иначе по ответу
    видно, что чужая точка существует.
    """
    page = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", page)
    page = re.sub(r'name="next" value="[^"]*"', "", page)
    return re.sub(r'value="[0-9a-f-]{36}"', 'value="…"', page)


def test_a_foreign_tenant_item_in_the_filter_reveals_nothing(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Статья чужого партнёра в фильтре — пусто, а не отказ и не подсказка."""
    other_tenant, other_item = _foreign_item(sql)
    login_as(client, "director")
    try:
        record(client, item, units, unit=units["NS1"], amount="100.00")
        page = body(client.get(LIST, {**WIDE, "item": other_item}))
        assert shown(page) == []
        assert total_of(page) == Decimal("0")
        assert "Чужая статья" not in page
    finally:
        client.post("/logout/")
        sql.execute("delete from expense_items where id = %s", (other_item,))
        sql.execute("delete from tenants where id = %s", (other_tenant,))


def _foreign_item(sql) -> tuple[str, str]:  # noqa: F811
    """Статья расходов другого партнёра — материал для проверки изоляции."""
    from psycopg.types.json import Jsonb

    tenant_id = sql.execute(
        """insert into tenants (code, title, country_code, base_currency, report_currency)
           values ('xx-test', 'Другой партнёр', 'XX', 'XXX', 'EUR') returning id"""
    ).fetchone()[0]
    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    item_id = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'alien', %s, %s, '2020-01-01') returning id""",
        (tenant_id, Jsonb({"ru": "Чужая статья"}), line),
    ).fetchone()[0]
    return str(tenant_id), str(item_id)


# --- правка и удаление --------------------------------------------------------


def test_the_edit_replaces_the_version_in_an_open_month(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Правка идёт заменой версии: старая строка остаётся историей."""
    login_as(client, "manager")
    try:
        key = record(client, item, units, amount="100.00")
        fact = fact_id_of(sql, key)

        answer = client.post(f"/expenses/{fact}/", {
            "date": august_day(), "amount": "150.00", "item": item,
            "note": "поправлено", "unit": units["NS1"], "ledger": "official",
        })
        assert answer.status_code == 302, body(answer)

        rows = facts_of(sql, key)
        assert [row[8] for row in rows] == [1, 2], f"замены не произошло: {rows}"
        assert rows[0][9] is not None and rows[0][10] == rows[1][12]
        assert str(rows[1][1]) == "150.00"
    finally:
        client.post("/logout/")


def test_the_deleted_expense_stays_visible_as_replaced(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Удаление не стирает строку: она остаётся видимой и выходит из итога.

    Сломайте удаление до `delete from facts` — и тест покраснеет: строка
    исчезнет из списка вовсе, а деньги пропадут без следа.
    """
    login_as(client, "manager")
    try:
        key = record(client, item, units, amount="100.00")
        fact = fact_id_of(sql, key)
        before = total_of(body(client.get(LIST, WIDE)))

        assert client.post(f"/expenses/{fact}/delete/").status_code == 302

        page = body(client.get(LIST, WIDE))
        states = {row["id"]: row["state"] for row in shown(page)}
        assert states.get(fact) == "removed", f"удалённая строка пропала: {states}"
        assert total_of(page) == before - Decimal("100.00"), "удалённое осталось в итоге"

        kept = sql.execute(
            "select superseded_at, superseded_by from facts where id = %s", (fact,)
        ).fetchone()
        assert kept[0] is not None and kept[1] is None, "строка не помечена удалённой"
    finally:
        client.post("/logout/")


def test_a_foreign_expense_cannot_be_opened_or_deleted(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Чужой расход не открывается и не удаляется, и ответ тот же, что у выдуманного."""
    import uuid

    login_as(client, "director")
    alien = fact_id_of(sql, record(client, item, units, unit=units["BG1"], amount="500.00"))
    client.post("/logout/")

    login_as(client, "manager")
    try:
        made_up = str(uuid.uuid4())
        assert client.get(f"/expenses/{alien}/").status_code == 404
        assert client.get(f"/expenses/{made_up}/").status_code == 404
        assert client.post(f"/expenses/{alien}/delete/").status_code == 404

        still_there = sql.execute(
            "select superseded_at from facts where id = %s", (alien,)
        ).fetchone()[0]
        assert still_there is None, "чужой расход удалён"
    finally:
        client.post("/logout/")


# --- правило 2: закрытый месяц не двигается -----------------------------------


def test_editing_an_expense_of_a_closed_month_does_not_move_it(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Правка расхода закрытого месяца: сторно и новая строка в текущем.

    Переписать строку закрытого месяца нельзя физически (`facts_guard`), а
    молча отказать — значило бы оставить бухгалтера с неверным числом навсегда.
    Поэтому исходная строка остаётся нетронутой, а в текущий месяц ложатся две:
    сторно на её сумму и исправленная запись. Закрытый месяц не двигается ни на
    копейку — это и проверяется числом до и после.
    """
    login_as(client, "manager")
    key = entry_key()
    assert client.post(
        NEW, payload(item, units, entry_key=key, date=JUNE_DAY, amount="1200.50")
    ).status_code == 302
    fact = fact_id_of(sql, key)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")
    before = june_total(sql)

    login_as(client, "manager")
    try:
        answer = client.post(f"/expenses/{fact}/", {
            "date": JUNE_DAY, "amount": "1300.00", "item": item,
            "note": "поправлено", "unit": units["NS1"], "ledger": "official",
        })
        assert answer.status_code == 302, body(answer)
        assert june_total(sql) == before, "закрытый месяц сдвинулся"

        corrections = sql.execute(
            """select amount, period from facts
                where dedup_key like %s and superseded_at is null order by amount""",
            (f"manual:cash:{key}#%",),
        ).fetchall()
        assert [str(amount) for amount, _ in corrections] == ["-1200.50", "1300.00"], (
            f"исправление не легло сторно и новой строкой: {corrections}"
        )
        assert {period for _, period in corrections} == {current_period()}

        # Итог по обеим сторонам сходится: в сумме исправление стоит ровно
        # разницу, а не двойную запись.
        assert sum(amount for amount, _ in corrections) == Decimal("99.50")
    finally:
        client.post("/logout/")


def test_deleting_an_expense_of_a_closed_month_is_a_storno(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Удаление из закрытого месяца — сторно в текущем, а не стирание строки."""
    login_as(client, "manager")
    key = entry_key()
    assert client.post(
        NEW, payload(item, units, entry_key=key, date=JUNE_DAY, amount="1200.50")
    ).status_code == 302
    fact = fact_id_of(sql, key)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")
    before = june_total(sql)

    login_as(client, "manager")
    try:
        assert client.post(f"/expenses/{fact}/delete/").status_code == 302
        assert june_total(sql) == before, "закрытый месяц сдвинулся"

        storno = sql.execute(
            """select amount, period from facts
                where dedup_key = %s and superseded_at is null""",
            (f"manual:cash:{key}#storno",),
        ).fetchall()
        assert [(str(amount), period) for amount, period in storno] == [
            ("-1200.50", current_period())
        ], f"сторно не записано: {storno}"

        original = sql.execute(
            "select superseded_at from facts where id = %s", (fact,)
        ).fetchone()[0]
        assert original is None, "строка закрытого месяца тронута"
    finally:
        client.post("/logout/")


def test_the_list_says_where_a_closed_month_expense_landed(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Расход июня, учтённый в августе, показывает оба месяца, а не один.

    Иначе бухгалтер ищет строку в июне, не находит и считает её потерянной.
    """
    from web.i18n import month_title

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "manager")
    try:
        assert client.post(
            NEW, payload(item, units, entry_key=entry_key(), date=JUNE_DAY)
        ).status_code == 302
        page = body(client.get(LIST, WIDE))
        assert JUNE_DAY in page, "дата расхода потерялась"
        assert month_title(current_period()) in page, "не сказано, где искать строку"
    finally:
        client.post("/logout/")


def test_an_expense_of_a_closed_month_cannot_be_edited_into_it(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Форма правки закрытого месяца честно говорит, что запись уйдёт в текущий."""
    from web.i18n import month_title

    login_as(client, "manager")
    key = entry_key()
    assert client.post(
        NEW, payload(item, units, entry_key=key, date=JUNE_DAY)
    ).status_code == 302
    fact = fact_id_of(sql, key)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "manager")
    try:
        page = body(client.get(f"/expenses/{fact}/"))
        assert month_title(date(2026, 6, 1)) in page
        assert month_title(current_period()) in page, "не сказано, куда ляжет правка"
    finally:
        client.post("/logout/")
