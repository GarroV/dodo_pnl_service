"""Один адрес, один `?ledger=` — один ответ у экрана и у вызова (T133).

Спека (`docs/forge/spec.md`, «API и будущая MCP-обёртка», условие 2) требует,
чтобы срез по регистру был **параметром запроса**, а не отдельным маршрутом: у
вызова тогда один адрес и один разбор ответа, а видимость по-прежнему решает
база. Продукт это условие выполнял наполовину: `?ledger=` читал только вызов по
HTTP, а экран разбирал лишь `from`, `to`, `unit` и `item` и молча показывал
полный список.

Итог был такой: `/expenses/?ledger=official` — две строки, а
`/api/expenses/?ledger=official` — одна. Это не утечка (экран шире, но в
пределах прав роли), это про доверие: человек, проверяющий ответ бота глазами по
тому же адресу, получал другое число и другой итог без единого слова объяснения.

Поэтому проверки здесь **сравнивают** два ответа, а не проверяют каждый по
отдельности. Проверка «оба непустые» зелена и у экрана, который игнорирует
параметр.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    current_period,
    entry_key,
    facts_of,
    facts_removed,
    item,
    payload,
    tenant,
    units,
)
from test_directory import payruns_restored, sql  # noqa: F401
from test_expenses_api import answer
from test_expenses_list import LIST, WIDE, record, shown, total_of

API = "/api/expenses/"


@pytest.fixture
def two_ledgers(client, sql, item, units, facts_removed):  # noqa: F811
    """Два расхода одной точки в разных регистрах — материал для сравнения."""
    login_as(client, "director")
    keys = {
        "official": record(
            client, item, units, unit=units["NS1"], amount="10.00", ledger="official",
        ),
        "internal": record(
            client, item, units, unit=units["NS1"], amount="90.00", ledger="internal",
        ),
    }
    client.post("/logout/")
    return keys


def screen(client, **params) -> tuple[list[str], str]:
    """Строки экрана: номера фактов и итог — то, что человек видит глазами."""
    page = body(client.get(LIST, {**WIDE, **params}))
    return [row["id"] for row in shown(page)], f"{total_of(page)}"


def called(client, **params) -> tuple[list[str], str]:
    """То же самое запросом: номера фактов и итог строкой."""
    response = client.get(API, {**WIDE, **params})
    assert response.status_code == 200, body(response)
    parsed = answer(response)
    return [row["id"] for row in parsed["rows"]], parsed["total"]


@pytest.mark.parametrize("cut", ["official", "internal", "supplementary", ""])
def test_the_screen_and_the_call_answer_the_same_ledger_cut(
    client, sql, two_ledgers, cut,  # noqa: F811
):
    """Один и тот же `?ledger=` даёт один и тот же список и один и тот же итог.

    Сравниваются номера показанных строк и итог — то есть ровно то, что человек
    сверяет глазами. Уберите разбор `ledger` из `filters_from`, и тест покраснеет
    на официальном срезе: экран покажет обе строки и итог 100,00, а вызов —
    одну и 10,00.
    """
    login_as(client, "director")
    try:
        params = {"ledger": cut} if cut else {}
        assert screen(client, **params) == called(client, **params)
    finally:
        client.post("/logout/")


def test_the_official_cut_hides_the_internal_row_on_the_screen(
    client, sql, two_ledgers,  # noqa: F811
):
    """Срез сужает и на экране: внутренняя строка из него уходит вместе с итогом.

    Отдельно от сравнения: сравнение доказывает, что два ответа одинаковы, но
    осталось бы зелёным, если бы **оба** перестали срез делать.
    """
    login_as(client, "director")
    try:
        rows, total = screen(client, ledger="official")
        assert len(rows) == 1 and total == "10.00", (rows, total)

        wide_rows, wide_total = screen(client)
        assert len(wide_rows) == 2 and wide_total == "100.00", (wide_rows, wide_total)
    finally:
        client.post("/logout/")


def test_an_invisible_ledger_looks_the_same_as_a_made_up_word_on_the_screen(
    client, sql, two_ledgers,  # noqa: F811
):
    """Невидимый регистр и выдуманное слово отвечают экрану одинаково — пустотой.

    Разный ответ означал бы, что перебором значений в адресе составляется список
    регистров партнёра, ни одного из них не увидев (D023). У вызова это уже
    проверено (`test_expenses_api`), у экрана — здесь.

    Сравниваются строки и итог, а не страница целиком: в разметке лежат ключ
    CSRF и адрес возврата, и они по построению разные у двух запросов.
    """
    login_as(client, "manager")
    try:
        hidden = screen(client, ledger="internal")
        invented = screen(client, ledger="no-such-ledger")
        assert hidden == invented == ([], "0"), (hidden, invented)

        # И ни одна из двух страниц не отвечает отказом: отказ был бы третьим,
        # отличимым ответом.
        for cut in ("internal", "no-such-ledger"):
            assert client.get(LIST, {**WIDE, "ledger": cut}).status_code == 200
    finally:
        client.post("/logout/")


def test_the_screen_offers_only_the_ledgers_the_role_sees(
    client, sql, two_ledgers,  # noqa: F811
):
    """В отборе экрана — только видимые роли регистры, и переключатель вообще есть."""
    login_as(client, "manager")
    try:
        page = body(client.get(LIST, WIDE))
        assert 'name="ledger"' in page, "срез по регистру с экрана недоступен вовсе"
        assert 'value="internal"' not in page, "предложен регистр, которого роль не видит"
    finally:
        client.post("/logout/")
