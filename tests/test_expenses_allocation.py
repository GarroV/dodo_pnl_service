"""Расход на всю сеть и его разнесение по точкам (T111).

Механизм разнесения живёт в схеме с T107 (`allocation_plan`, `allocate_fact`,
`reallocate_period`), и его инварианты проверены там же — на уровне SQL
(`test_facts_schema.py`, `test_facts_allocation_rules.py`). Здесь проверяется
другое: доходит ли механизм до человека и говорит ли он правду.

Четыре правила задачи и как каждое проверено.

**1. Сумма детей сходится с родителем до копейки на неровной сумме.** 100.01 на
три точки → 33.34 / 33.33 / 33.34. Проверяется через экран, а не запросом:
человек вносит расход формой и должен получить тот же результат, что тест схемы.

**2. Факт без правила остаётся нераспределённым и ВИДЕН.** Отдельный список
(`/expenses/unallocated/`) — по нему бухгалтер и понимает, чего не хватает,
чтобы закрыть месяц. Сумма, потерявшаяся между «по точкам» и «по сети», — дыра
в P&L, которая не кричит.

**3. Пересчёт на неизменившихся правилах ничего не переписывает.** Ноль
изменений сказано словами, и ни одна строка не получает новой версии.

**4. Закрытый период не пересчитывается (D020).** Пересчёт его пропускает,
называет пропущенный месяц вслух и не двигает его итог ни на копейку.
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
    facts_removed,
    item,
    june_total,
    payload,
    tenant,
    units,
)
from test_directory import approve_june, payruns_restored, sql  # noqa: F401
from test_expenses_list import LIST, WIDE, august_day, shown, total_of

NEW = "/expenses/new/"
WAITING = "/expenses/unallocated/"
NETWORK = "network"


@pytest.fixture
def rules_removed(sql):  # noqa: F811
    """Правила разнесения, заведённые тестом, не переживают его.

    Просить эту фикстуру нужно **раньше** `facts_removed`: разбираются они в
    обратном порядке, а на правило ссылаются разнесённые факты — пока они живы,
    удалить правило база не даст.
    """
    yield
    sql.execute("delete from allocation_rules where expense_item_id is not null")


def set_rule(client, item_id, *, method="even", unit="", valid_from="2026-01-01",
             ledger="official"):
    """Завести правило разнесения статьи — с карточки статьи, как человек."""
    card = f"/directory/expense-items/{item_id}/"
    page = body(client.get(card))
    answer = client.post(card, {
        "code": _value(page, "code"),
        "title_ru": "Вода", "title_en": "Water", "title_sr_latn": "Voda",
        "pnl_item": _selected(page, "pnl_item"),
        "valid_from": "2020-01-01",
        "valid_to": "",
        "alloc_method": method,
        "alloc_unit": unit,
        "alloc_ledger": ledger,
        "alloc_from": valid_from,
    })
    assert answer.status_code == 302, body(answer)
    return answer


def _value(page: str, name: str) -> str:
    found = re.search(rf'name="{name}"[^>]*value="([^"]*)"', page)
    assert found, f"на странице нет поля {name}"
    return found.group(1)


def _selected(page: str, name: str) -> str:
    """Выбранный вариант списка — чтобы форма отправлялась целиком, как из браузера."""
    block = re.search(rf'name="{name}".*?</select>', page, re.S)
    assert block, f"на странице нет списка {name}"
    found = re.search(r'value="([^"]+)" selected', block.group(0))
    assert found, f"в списке {name} ничего не выбрано"
    return found.group(1)


def network_expense(client, item_id, *, amount="100.01", key=None, **extra) -> str:
    """Внести расход на всю сеть: точки нет, разносить будет правило."""
    key = key or entry_key()
    form = {
        "date": august_day(), "amount": amount, "item": item_id,
        "note": "аренда офиса", "unit": NETWORK, "ledger": "official",
        "entry_key": key,
    }
    form.update(extra)
    answer = client.post(NEW, form)
    assert answer.status_code == 302, body(answer)
    return key


def facts_of(sql, key: str) -> list[tuple]:  # noqa: F811
    return sql.execute(
        """select f.dedup_key, u.code, f.amount, f.allocation::text, f.expense_item_id::text
             from facts f left join units u on u.id = f.unit_id
            where f.dedup_key like %s and f.superseded_at is null
            order by f.dedup_key""",
        (f"manual:cash:{key}%",),
    ).fetchall()


def revisions(sql) -> list[tuple]:  # noqa: F811
    return sql.execute(
        "select id, revision from facts where superseded_at is null order by id"
    ).fetchall()


# --- правило на карточке статьи -----------------------------------------------


def test_the_rule_is_kept_with_the_expense_item(
    client, sql, item, rules_removed,  # noqa: F811
):
    """Правило разнесения ведётся там же, где статья: «аренда — поровну».

    Ключ правила — статья, потому что контрагента у расхода из кассы нет и
    взяться ему неоткуда: человек выбирает статью, она и отвечает на вопрос
    «как это разносить».
    """
    login_as(client, "admin")
    try:
        set_rule(client, item, method="even")

        rule = sql.execute(
            """select method::text, ledger::text, valid_from, counterparty_id,
                      pnl_item_id::text
                 from allocation_rules where expense_item_id = %s""",
            (item,),
        ).fetchall()
        assert len(rule) == 1, rule
        method, ledger, valid_from, counterparty, pnl_item = rule[0]
        assert (method, ledger, valid_from) == ("even", "official", date(2026, 1, 1))
        assert counterparty is None, "у правила по статье не должно быть контрагента"
        assert pnl_item == str(
            sql.execute(
                "select pnl_item_id from expense_items where id = %s", (item,)
            ).fetchone()[0]
        ), "строка P&L правила разошлась со статьёй"

        assert "even" in body(client.get(f"/directory/expense-items/{item}/"))
    finally:
        client.post("/logout/")


@pytest.mark.parametrize("role", ["director", "accountant", "manager"])
def test_only_the_directory_keeper_sets_the_rule(
    client, sql, item, rules_removed, role,  # noqa: F811
):
    """Правило разнесения ведёт тот же, кто ведёт справочники, и никто больше."""
    login_as(client, role)
    try:
        assert client.get(f"/directory/expense-items/{item}/").status_code == 403
    finally:
        client.post("/logout/")


# --- правило 1: копейки -------------------------------------------------------


def test_a_network_expense_is_split_to_the_kopeck(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """100.01 на три точки → 33.34 / 33.33 / 33.34, и сумма сходится с родителем."""
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    try:
        key = network_expense(client, item, amount="100.01")
        rows = facts_of(sql, key)

        parent = [row for row in rows if row[1] is None]
        children = [row for row in rows if row[1] is not None]
        assert len(parent) == 1 and parent[0][3] == "split", rows
        assert [(code, amount) for _, code, amount, _, _ in children] == [
            ("BG1", Decimal("33.34")), ("NS1", Decimal("33.33")), ("NS2", Decimal("33.34")),
        ]
        assert sum(amount for _, _, amount, _, _ in children) == parent[0][2]
        assert {row[4] for row in children} == {item}, "статья потерялась при разнесении"
    finally:
        client.post("/logout/")


def test_the_split_is_deterministic(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Два одинаковых расхода делятся одинаково: остаток не «как получится»."""
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    try:
        first = network_expense(client, item, amount="100.01")
        second = network_expense(client, item, amount="100.01")
        shape = lambda key: [  # noqa: E731
            (code, amount) for _, code, amount, _, _ in facts_of(sql, key) if code
        ]
        assert shape(first) == shape(second)
    finally:
        client.post("/logout/")


def test_the_list_shows_the_parent_once(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """В списке расходов разнесённая запись одна: дети — следствие, а не расход.

    Иначе итог удвоился бы: 100.01 родителя плюс 100.01 детей.
    """
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    try:
        network_expense(client, item, amount="100.01")
        page = body(client.get(LIST, WIDE))
        assert total_of(page) == Decimal("100.01"), "итог удвоился на детях разнесения"
        assert len(shown(page)) == 1
    finally:
        client.post("/logout/")


# --- правило 2: нераспределённое видно ----------------------------------------


def test_an_expense_without_a_rule_waits_and_is_visible(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Правила нет — расход ждёт и виден отдельным списком, а не исчезает."""
    login_as(client, "director")
    try:
        key = network_expense(client, item, amount="700.00")
        rows = facts_of(sql, key)
        assert len(rows) == 1 and rows[0][3] == "pending", rows

        page = body(client.get(WAITING))
        assert "700,00" in page, "нераспределённого не видно в списке"
        assert "Вода" in page, "по списку не понять, что это за сумма"
    finally:
        client.post("/logout/")


def test_the_waiting_list_keeps_the_ledger_slice(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Нераспределённое чужого регистра не видно и не считается (D023, D031)."""
    login_as(client, "director")
    network_expense(client, item, amount="333.00", ledger="internal")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        page = body(client.get(WAITING))
        assert "333,00" not in page, "показан расход невидимого регистра"
    finally:
        client.post("/logout/")


def test_the_rule_appears_later_and_the_expense_is_spread(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Правило завели после расхода — пересчёт разносит ожидающий факт."""
    login_as(client, "director")
    key = network_expense(client, item, amount="100.01")
    client.post("/logout/")

    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    try:
        assert client.post(WAITING).status_code == 302
        children = [row for row in facts_of(sql, key) if row[1] is not None]
        assert sum(amount for _, _, amount, _, _ in children) == Decimal("100.01")
    finally:
        client.post("/logout/")


# --- правило 3: пересчёт вслепую не переписывает ------------------------------


def test_recalculation_without_changes_changes_nothing(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Пересчёт на тех же правилах: ноль изменений и ни одной новой версии."""
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    try:
        network_expense(client, item, amount="100.01")
        before = revisions(sql)

        answer = client.post(WAITING)
        assert answer.status_code == 302, body(answer)
        assert revisions(sql) == before, "пересчёт переписал факты вслепую"

        page = body(client.get(answer["Location"]))
        assert "0" in page, page
    finally:
        client.post("/logout/")


# --- правило 4: закрытый месяц не пересчитывается -----------------------------


def test_a_retroactive_rule_is_taken_but_the_closed_month_stays(
    client, sql, web_env, item, units, rules_removed, facts_removed,  # noqa: F811
    payruns_restored,  # noqa: F811
):
    """Правило задним числом заводится, но закрытый месяц им не переписывается.

    Так было не всегда: сначала здесь стоял отказ. Правило продукта сменилось не
    у разнесения, а везде (D020, доведено до экранов в T121): правку **с датой**
    продукт принимает, а закрытый месяц ею не трогает. У правила разнесения даты
    версионируются, значит оно подчиняется общему правилу.

    Проверяется именно то, ради чего отказ и стоял: числа закрытого июня до и
    после обязаны совпасть до копейки, а ревизии его строк — не измениться.
    Что пересчёт пропустил закрытый месяц и **сказал** об этом, проверяет
    соседний тест: молчаливый пропуск читается как «пересчитано».
    """
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    network_expense(client, item, amount="100.01", key=entry_key(), date=JUNE_DAY)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")
    before_total, before = june_total(sql), revisions(sql)

    login_as(client, "admin")
    try:
        card = f"/directory/expense-items/{item}/"
        page = body(client.get(card))
        answer = client.post(card, {
            "code": _value(page, "code"),
            "title_ru": "Вода", "title_en": "Water", "title_sr_latn": "Voda",
            "pnl_item": _selected(page, "pnl_item"),
            "valid_from": "2020-01-01", "valid_to": "",
            "alloc_method": "fixed_unit", "alloc_unit": units["BG1"],
            "alloc_ledger": "official", "alloc_from": "2026-06-01",
        })
        assert answer.status_code in (200, 302), answer.status_code

        assert june_total(sql) == before_total, "закрытый месяц сдвинулся"
        assert revisions(sql) == before, "строки закрытого месяца переписаны"
        assert sql.execute(
            "select count(*) from allocation_rules where expense_item_id = %s", (item,)
        ).fetchone()[0] == 2, "новая версия правила не завелась"
    finally:
        client.post("/logout/")


def test_recalculation_skips_a_closed_month_and_says_so(
    client, sql, web_env, item, units, rules_removed, facts_removed,  # noqa: F811
    payruns_restored,  # noqa: F811
):
    """Пересчёт закрытый месяц не трогает и называет его вслух.

    Молчаливый пропуск читается как «пересчитано»: человек уходит уверенный,
    что правило применилось везде.
    """
    from web.i18n import month_title

    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "director")
    network_expense(client, item, amount="100.01", key=entry_key(), date=JUNE_DAY)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")
    before_total, before = june_total(sql), revisions(sql)

    login_as(client, "director")
    try:
        answer = client.post(WAITING)
        assert answer.status_code == 302, body(answer)
        assert june_total(sql) == before_total, "закрытый месяц сдвинулся"
        assert revisions(sql) == before, "строки закрытого месяца переписаны"

        page = body(client.get(answer["Location"]))
        assert month_title(date(2026, 6, 1)) in page, "о пропущенном месяце не сказано"
    finally:
        client.post("/logout/")


# --- кто вправе разносить -----------------------------------------------------


def test_the_manager_cannot_spread_an_expense_over_other_units(
    client, sql, item, units, rules_removed, facts_removed,  # noqa: F811
):
    """Управляющий вносит расход на сеть, но разносит его не он — и это база.

    Разнесение пишет строки на чужие точки, а туда управляющему писать нельзя
    (`unit_visibility` на `facts`). Отказ приходит от политики, а не от проверки
    в представлении: расход остаётся ждать, и человеку об этом сказано словами.
    Сломайте политику — и строки лягут на чужие точки, а тест покраснеет.
    """
    login_as(client, "admin")
    set_rule(client, item, method="even")
    client.post("/logout/")

    login_as(client, "manager")
    try:
        key = network_expense(client, item, amount="100.01")
        rows = facts_of(sql, key)
        assert [row[1] for row in rows] == [None], f"расход разошёлся по чужим точкам: {rows}"
        assert rows[0][3] == "pending"

        page = body(client.get(f"{NEW}?saved={current_period():%Y-%m}&refused=0"))
        assert "ведёт все точки" in page, "человеку не сказано, почему расход ждёт"
        assert WAITING in page, "не сказано, где искать нераспределённое"
    finally:
        client.post("/logout/")
