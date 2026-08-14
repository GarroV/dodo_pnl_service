"""Разрез по регистру знает и о расходах, а не только о зарплате (T137).

Что было. Словарь разрезов собирался из того, что встретилось **в ведомости**
(`reports.sheet.slice_cells`: `available = shown_ledgers(whole)`). Пока у периода
был один источник строк, «регистры ведомости» и «регистры месяца» были одним и
тем же. С появлением расходов — перестали: в месяце бывает трата во внутреннем
регистре и ни одной зарплатной строки этого регистра. Тогда `?ledger=internal`
схлопывался во «все видимые», и разрезом такую трату было не отделить: в файл
одного регистра приезжали чужие (issue #108). Ни утечки, ни неверных чисел —
выбор человека просто молча не срабатывал.

Условие здесь построено намеренно: июнь **посчитан**, но зарплатные строки
внутреннего регистра из него убраны. Так выглядит партнёр, у которого во
внутреннем регистре живут только траты наличными, — и так дефект виден, не
превращаясь в «периода нет вовсе».

Что проверяется и чего не проверяет ни один соседний файл:

**1. Разрез существует, когда его источник — только расходы.** И на экране
(кнопка), и в файле (сужение). Одно без другого бесполезно: невидимую кнопку
человек не нажмёт, а нажатую — обязан получить.

**2. Части сходятся с целым.** Сумма файлов по разрезам равна файлу без разреза.
Налоговые строки из счёта исключены, и это не подгонка: налог посчитан по строке
ведомости целиком, регистра у него нет, и ни в один разрез он не входит — ровно
то, о чём файл говорит вслух после T141.

**3. Разрез не рассказывает о том, чего роли не видно.** Регистры расходов
спрашиваются у `pnl_lines`, а это `security_invoker`-представление: трата
внутреннего регистра до управляющего не доезжает, и кнопки с названием этого
регистра у него нет. Пустая кнопка — тот самый «след» из D023.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal

import openpyxl
import pytest

from conftest import body, login_as, wipe_payruns
from test_cash_expense import (  # noqa: F401
    JUNE_DAY,
    entry_key,
    facts_removed,
    item,
    payload,
    tenant,
    units,
)
from test_directory import payruns_restored, sql  # noqa: F401

D = Decimal
JUNE = "2026-06-01"
NEW = "/expenses/new/"

# Строки файла, у которых есть регистр. Налог и взносы не в счёт: они посчитаны
# по строке ведомости целиком, и регистра у них нет вовсе (см. шапку модуля).
CUT_KINDS = ("Начисление", "Расход")


def spend(client, item_id, units_map, **extra) -> None:
    form = payload(item_id, units_map, entry_key=entry_key(), date=JUNE_DAY, **extra)
    answer = client.post(NEW, form)
    assert answer.status_code == 302, body(answer)


def page_url(sql, tenant) -> str:  # noqa: F811
    """Страница именно июня: со списка периодов приезжает «первый попавшийся»
    месяц, и проверка зависела бы от того, какой сегодня день."""
    period_id = sql.execute(
        "select id from periods where tenant_id = %s and period = %s", (tenant, JUNE)
    ).fetchone()[0]
    return f"/periods/{period_id}/"


def export_url(sql, tenant) -> str:  # noqa: F811
    return page_url(sql, tenant) + "export/pnl/"


def offered_cuts(client, sql, tenant) -> set[str]:  # noqa: F811
    """Разрезы, которые продукт предлагает сам, — с его же экрана."""
    return set(re.findall(r"ledger=([a-z]+)", body(client.get(page_url(sql, tenant)))))


def file_rows(client, url, kinds=CUT_KINDS) -> list[tuple]:
    response = client.get(url)
    assert response.status_code == 200, response.status_code
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    return [row for row in sheet.iter_rows(values_only=True) if row and row[3] in kinds]


def money(rows) -> Decimal:
    return sum((D(str(row[5])) for row in rows), D(0))


@pytest.fixture
def internal_only_in_expenses(
    client, web_env, sql, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Июнь посчитан, зарплаты во внутреннем регистре нет, трата в нём есть."""
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(
        page_url(sql, tenant_of(sql)) + "calculate/", {"inline": "1"}, follow=True
    ).status_code == 200
    # Зарплатные строки внутреннего регистра убираются владельцем схемы: экрана,
    # которым партнёр «не платит внутренним», не существует, а условие настоящее.
    sql.execute(
        "delete from pay_components where ledger = 'internal' and tenant_id in "
        "(select id from tenants where code = 'rs-dev')"
    )
    spend(client, item, units, unit=units["NS1"], amount="50.00", ledger="official")
    spend(client, item, units, unit=units["NS1"], amount="70.00", ledger="internal")
    yield
    client.post("/logout/")


def tenant_of(sql):  # noqa: F811
    return sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]


def test_a_cut_narrows_the_file_even_when_only_expenses_live_in_that_ledger(
    client, sql, tenant, internal_only_in_expenses,  # noqa: F811
):
    """Разрез внутреннего регистра отделяет трату, которой нет в зарплате."""
    url = export_url(sql, tenant)

    internal = file_rows(client, url + "?ledger=internal")
    assert internal, "разрез внутреннего регистра не отдал ни строки"
    assert money(internal) == D("70.00"), internal
    assert {row[2] for row in internal} == {"Внутренний"}, internal

    official = file_rows(client, url + "?ledger=official")
    assert {row[2] for row in official} == {"Официальный"}, official
    assert D("50.00") in [D(str(row[5])) for row in official], official


def test_the_parts_add_up_to_the_whole(client, sql, tenant, internal_only_in_expenses):  # noqa: F811
    """Сумма файлов по разрезам равна файлу без разреза — до копейки.

    Разрезы берутся с экрана, а не перечисляются здесь: разрез, которого продукт
    не предлагает, схлопывается во «все видимые» (D023, и это правильно), и в
    сумме такой файл считался бы вторым целым.
    """
    url = export_url(sql, tenant)

    whole = money(file_rows(client, url))
    parts = sum(
        (money(file_rows(client, f"{url}?ledger={code}"))
         for code in offered_cuts(client, sql, tenant)),
        D(0),
    )
    assert parts == whole, f"части {parts}, целое {whole}"


def test_the_switcher_offers_the_ledger_that_only_expenses_reached(
    client, sql, tenant, internal_only_in_expenses,  # noqa: F811
):
    """Кнопка разреза есть на экране: выбрать невыбираемое человек не может."""
    offered = offered_cuts(client, sql, tenant)
    assert offered == {"official", "supplementary", "internal"}, offered


def test_the_manager_is_not_told_about_the_ledger_he_cannot_see(
    client, sql, tenant, internal_only_in_expenses,  # noqa: F811
):
    """Роль без внутреннего регистра не получает ни его кнопки, ни его денег (D023)."""
    client.post("/logout/")
    login_as(client, "manager")
    try:
        offered = offered_cuts(client, sql, tenant)
        assert "internal" not in offered, f"управляющему назвали скрытый регистр: {offered}"

        spent = [
            row for row in file_rows(client, export_url(sql, tenant), kinds=("Расход",))
        ]
        assert money(spent) == D("50.00"), spent
    finally:
        client.post("/logout/")
        login_as(client, "director")
