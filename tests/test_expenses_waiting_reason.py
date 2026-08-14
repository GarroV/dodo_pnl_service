"""Почему расход ждёт разнесения — и почему продукт обязан назвать это верно (T132).

Из четырёх способов разнесения работают два: «Поровну между точками» и «На одну
точку». Ещё два выбрать можно, а исполнить нечем: «Пропорционально выручке» —
потому что выручки в продукте пока нет вовсе (она придёт с коннектором Dodo IS,
шестая очередь), «Спрашивать каждый раз» — потому что спрашивать по замыслу
должен человек, а точку он указывает руками в самом расходе.

До этой задачи оба случая продукт объяснял одной фразой: **«Правила разнесения у
статьи нет.»** Правило при этом лежало в `allocation_rules` и было видно в
карточке статьи. То есть человек, настроивший правило, читал, что правила нет, —
и дальше либо заводил его второй раз, либо решал, что сломан справочник.

Здесь проверяется ровно это: **фраза, отрицающая существующее правило, не
появляется ни на одном экране**, а вместо неё названа настоящая причина. Причина
считается базой (`allocation_reason`), а не собирается в представлении второй
копией поиска правила: копия разошлась бы с `allocation_plan` молча — и тогда
продукт снова объяснял бы ожидание неправильно, только другими словами.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    current_period,
    entry_key,
    facts_removed,
    item,
    payload,
    tenant,
    units,
)
from test_directory import payruns_restored, sql  # noqa: F401
from test_expenses_allocation import (  # noqa: F401
    NEW,
    WAITING,
    facts_of,
    network_expense,
    rules_removed,
    set_rule,
)

# Фраза, которой продукт отрицал существующее правило. Проверяется буквально:
# она обязана исчезнуть с экранов там, где правило есть.
DENIAL = "Правила разнесения у статьи нет"


def spread_answer(client, item_id, *, method, amount="700.00") -> str:
    """Завести правило, внести по нему расход на сеть и вернуть ответ продукта."""
    login_as(client, "admin")
    set_rule(client, item_id, method=method)
    client.post("/logout/")

    login_as(client, "director")
    answer = client.post(NEW, {
        "date": current_period().replace(day=5).isoformat(),
        "amount": amount, "item": item_id, "note": "аренда офиса",
        "unit": "network", "ledger": "official", "entry_key": entry_key(),
    })
    assert answer.status_code == 302, body(answer)
    return body(client.get(answer["Location"]))


# --- правило есть, исполнить его нечем ----------------------------------------


def test_a_by_revenue_rule_is_not_called_a_missing_rule(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """«Пропорционально выручке»: продукт называет выручку, а не отрицает правило.

    Правило лежит в базе и видно в карточке статьи. Сказать «правила нет» —
    значит отправить человека заводить его второй раз.
    """
    try:
        said = spread_answer(client, item, method="by_revenue")
        assert DENIAL not in said, "продукт отрицает правило, которое сам же хранит"
        assert "выручк" in said.lower(), f"настоящая причина не названа:\n{said}"
        assert "коннектор" in said.lower(), "не сказано, откуда возьмётся выручка"
    finally:
        client.post("/logout/")


def test_an_ask_rule_says_where_to_choose_the_unit(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """«Спрашивать каждый раз»: сказано, что точку выбирают руками и где именно.

    Обещание «спрашивать» продукт выполнить не может — экрана вопроса нет. Зато
    точка правится в самом расходе, и человека нужно вести туда, а не оставлять
    с фразой про отсутствующее правило.
    """
    try:
        said = spread_answer(client, item, method="ask")
        assert DENIAL not in said, "продукт отрицает правило, которое сам же хранит"
        assert "руками" in said.lower(), f"не сказано, что точку выбирают руками:\n{said}"
    finally:
        client.post("/logout/")


def test_without_a_rule_the_product_still_says_there_is_no_rule(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Правила действительно нет — и тогда прежняя фраза верна и остаётся."""
    login_as(client, "director")
    try:
        key = network_expense(client, item, amount="700.00")
        assert [row[3] for row in facts_of(sql, key)] == ["pending"]
        said = body(client.get(f"{NEW}?saved={current_period():%Y-%m}&waiting=no_rule"))
        assert DENIAL in said, said
    finally:
        client.post("/logout/")


# --- экран нераспределённых ---------------------------------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [("by_revenue", "выручк"), ("ask", "руками"), ("", "Правила разнесения")],
)
def test_the_waiting_list_says_why_each_row_waits(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
    method, expected,
):
    """У каждой ждущей строки написано, что именно мешает её разнести.

    Без этого список отвечает на «сколько висит», но не на «почему», — а чинят
    как раз причину.
    """
    if method:
        login_as(client, "admin")
        set_rule(client, item, method=method)
        client.post("/logout/")

    login_as(client, "director")
    try:
        network_expense(client, item, amount="700.00")
        page = body(client.get(WAITING))
        assert expected.lower() in page.lower(), f"причина ожидания не названа:\n{page}"
    finally:
        client.post("/logout/")


def test_the_recalculation_does_not_claim_there_was_nothing_to_do(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Кнопка «Разнести по правилам» при висящих суммах не говорит «нечего менять».

    Это была вторая половина той же неправды: 1 490,00 висят, а продукт отвечает
    «менять было нечего» — то есть человек уходит уверенный, что всё разнесено.
    """
    login_as(client, "admin")
    set_rule(client, item, method="by_revenue")
    client.post("/logout/")

    login_as(client, "director")
    try:
        network_expense(client, item, amount="700.00")
        answer = client.post(WAITING)
        assert answer.status_code == 302, body(answer)

        page = body(client.get(answer["Location"]))
        assert "менять было нечего" not in page, page
        assert "выручк" in page.lower(), f"о причине по-прежнему молчат:\n{page}"
    finally:
        client.post("/logout/")


def test_the_manager_is_not_told_to_press_a_button_that_refuses_him(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Управляющему причина не выводится из плана: его план заведомо неверен.

    `allocation_plan` читает `units` под политиками вызвавшего, а у управляющего
    список точек короче. Правило «поровну» дало бы ему план из одной строки —
    то есть причину «правило есть, нажмите «Разнести по правилам»», — а кнопка
    ему откажет. Поэтому там, где причина считается по плану, ему отвечают тем,
    что верно всегда: разносит тот, кто ведёт все точки.
    """
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        network_expense(client, item, amount="700.00")
        page = body(client.get(WAITING))
        assert "Разнести по правилам»" not in page, (
            "управляющему обещана кнопка, которая ему откажет"
        )
        assert "ведёт все точки" in page, f"причина названа неверно:\n{page}"
    finally:
        client.post("/logout/")


# --- вызов по HTTP отвечает тем же --------------------------------------------


def test_the_api_names_the_same_reason_as_the_screen(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """У вызова причина та же и разбираемая машиной, а не только словами."""
    import json

    login_as(client, "admin")
    set_rule(client, item, method="by_revenue")
    client.post("/logout/")

    login_as(client, "director")
    try:
        answer = client.post(
            "/api/expenses/",
            json.dumps({
                "date": current_period().replace(day=5).isoformat(),
                "amount": "700.00", "item": item, "unit": "network",
                "ledger": "official", "note": "аренда офиса",
                "entry_key": entry_key(),
            }),
            content_type="application/json",
        )
        assert answer.status_code == 200, body(answer)
        spread = json.loads(answer.content.decode())["allocation"]
        assert spread["state"] == "waiting", spread
        assert spread["reason"] == "no_revenue", spread
    finally:
        client.post("/logout/")
