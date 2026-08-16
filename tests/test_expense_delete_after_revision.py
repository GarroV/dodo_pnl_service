"""Удаление расхода закрытого месяца ПОСЛЕ его правки (T154, находка Н3 сверки 8).

Единственный случай за всю сверку, когда **деньги остаются в P&L после того, как
человек их удалил**. Ход обычный для бухгалтера: поправил сумму в закрытом
месяце (продукт положил в текущий сторно и исправленную строку), потом понял, что
документа не было вовсе, и нажал «Удалить расход». Продукт отвечал 302 и
возвращал в список — молча, а исправленная строка оставалась живой в текущем
месяце и уезжала в файл «Строки для P&L».

Причина была в идемпотентности: `remove_expense` звал `storno_expense` с тем же
ключом (`#storno`), который уже завела правка, `upsert_fact` видел ту же строку и
не делал ничего, а `#fix`-строку на новую сумму никто не снимал.

**Проверяется числом, а не словами.** Главный тест смотрит сумму действующих
фактов до расхода и после удаления: она обязана вернуться к исходной. Это тот
самый вопрос, ради которого задача заведена, — «остались ли деньги в P&L».

Слова проверяются рядом и отдельно: молчаливое «ничего не произошло» на кнопке
удаления недопустимо ни в каком случае, включая повторное нажатие.
"""
from __future__ import annotations

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
from test_expenses_list import NEW, fact_id_of

DELETE = "/expenses/%s/delete/"


def live_total(sql, tenant) -> Decimal:  # noqa: F811
    """Сумма всех действующих фактов партнёра — то самое, что уедет в P&L.

    Целиком, а не по одному месяцу: правка закрытого месяца раскидывает строки по
    двум месяцам сразу, и месячный итог по отдельности сошёлся бы при потерянных
    деньгах в соседнем.
    """
    return sql.execute(
        "select coalesce(sum(amount), 0) from facts "
        "where tenant_id = %s and superseded_at is null",
        (tenant,),
    ).fetchone()[0]


def corrections(sql, key: str) -> list[tuple[str, date]]:  # noqa: F811
    """Действующие строки-исправления этой записи: сумма и месяц учёта."""
    return [
        (str(amount), period)
        for amount, period in sql.execute(
            """select amount, period from facts
                where dedup_key like %s and superseded_at is null
                order by amount""",
            (f"manual:cash:{key}#%",),
        ).fetchall()
    ]


@pytest.fixture
def revised(client, sql, web_env, item, units, facts_removed, payruns_restored):  # noqa: F811
    """Расход июня, внесённый до закрытия месяца и поправленный после.

    Заготовка ровно та, на которой находка воспроизведена: 500,00 в закрытом
    июне, правка на 700,00 → сторно −500,00 и исправление +700,00 в текущем
    месяце.
    """
    login_as(client, "director")
    key = entry_key()
    assert client.post(
        NEW, payload(item, units, entry_key=key, date=JUNE_DAY, amount="500.00",
                     unit=units["NS1"], note="ПОДОПЫТНЫЙ"),
    ).status_code == 302
    fact = fact_id_of(sql, key)
    client.post("/logout/")

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "director")
    try:
        assert client.post(f"/expenses/{fact}/", {
            "date": JUNE_DAY, "amount": "700.00", "item": item,
            "unit": units["NS1"], "ledger": "official", "note": "ПОДОПЫТНЫЙ правленый",
        }).status_code == 302
        assert corrections(sql, key) == [
            ("-500.00", current_period()), ("700.00", current_period()),
        ], "заготовка не сложилась: правка не легла сторно и исправлением"
        yield {"key": key, "fact": fact}
    finally:
        client.post("/logout/")


# =============================================================================
# 1. Деньги. Главная проверка задачи
# =============================================================================


def test_deleting_a_revised_expense_takes_the_money_out_of_the_pnl(
    client, sql, tenant, revised,  # noqa: F811
):
    """ГЛАВНАЯ ПРОВЕРКА: после удаления денег этого расхода в P&L нет.

    Считается числом: сумма действующих фактов после удаления обязана стать
    такой же, как если бы расхода не вносили вовсе, — то есть меньше на все
    700,00 исправленной строки. До починки она не двигалась ни на копейку.
    """
    before = live_total(sql, tenant)
    login_as(client, "director")
    try:
        answer = client.post(DELETE % revised["fact"])
        assert answer.status_code == 302, body(answer)
    finally:
        client.post("/logout/")

    after = live_total(sql, tenant)
    assert after == before - Decimal("700.00"), (
        f"деньги остались в P&L после удаления: было {before}, стало {after}"
    )


def test_the_closed_month_does_not_move_when_a_revised_expense_is_deleted(
    client, sql, revised,  # noqa: F811
):
    """Удаление не трогает закрытый июнь: он остаётся прежним до копейки."""
    before = june_total(sql)
    login_as(client, "director")
    try:
        assert client.post(DELETE % revised["fact"]).status_code == 302
    finally:
        client.post("/logout/")
    assert june_total(sql) == before, "закрытый месяц сдвинулся"

    original = sql.execute(
        "select superseded_at from facts where id = %s", (revised["fact"],)
    ).fetchone()[0]
    assert original is None, "строка закрытого месяца тронута"


def test_the_correction_row_is_withdrawn_and_the_storno_stays(
    client, sql, revised,  # noqa: F811
):
    """Из двух строк исправления остаётся сторно, а исправленная снимается.

    Именно так, а не наоборот: сторно — единственное, что отменяет нетронутую
    строку закрытого месяца. Снять его значило бы вернуть в P&L исходные 500,00.
    """
    login_as(client, "director")
    try:
        assert client.post(DELETE % revised["fact"]).status_code == 302
    finally:
        client.post("/logout/")

    assert corrections(sql, revised["key"]) == [("-500.00", current_period())], (
        "после удаления остались не те строки исправления"
    )


# =============================================================================
# 2. Слова. Молчаливого «ничего не произошло» быть не должно
# =============================================================================


def test_the_screen_says_what_the_deletion_did(client, sql, revised):  # noqa: F811
    """Человек читает, что расход удалён и куда легло сторно."""
    login_as(client, "director")
    try:
        answer = client.post(DELETE % revised["fact"])
        page = body(client.get(answer["Location"]))
    finally:
        client.post("/logout/")

    assert "удал" in page.lower(), f"после удаления экран молчит:\n{page[:2000]}"


def test_deleting_the_same_expense_twice_is_not_a_silent_no_op(
    client, sql, tenant, revised,  # noqa: F811
):
    """Повторное удаление не молчит и денег больше не двигает.

    Второе нажатие — обычное дело: строка закрытого месяца остаётся в списке
    живой (тронуть её нельзя), и человек нажимает ещё раз. Ответ обязан сказать,
    что расход уже удалён, а не увести в список как после успеха.
    """
    login_as(client, "director")
    try:
        assert client.post(DELETE % revised["fact"]).status_code == 302
        after_first = live_total(sql, tenant)

        answer = client.post(DELETE % revised["fact"])
        page = body(client.get(answer["Location"]))
    finally:
        client.post("/logout/")

    assert live_total(sql, tenant) == after_first, "второе удаление сдвинуло деньги"
    assert "уже удал" in page.lower(), (
        f"повторное удаление прошло молча:\n{page[:2000]}"
    )


# =============================================================================
# 3. Вторая ручка той же двери: вызов по HTTP (T112)
# =============================================================================


def test_the_api_deletion_takes_the_same_money_out(client, sql, tenant, revised):  # noqa: F811
    """Вызов по HTTP удаляет ровно то же, что кнопка, и называет состояние.

    Дверь одна, ручки две: починка, сделанная только на экране, оставила бы
    деньги в P&L у всех, кто ходит вызовом, — и узнать об этом было бы неоткуда.
    """
    import json

    before = live_total(sql, tenant)
    login_as(client, "director")
    try:
        first = client.post(f"/api/expenses/{revised['fact']}/delete/")
        assert first.status_code == 200, body(first)
        said = json.loads(first.content)
        assert said["state"] == "storno", said
        assert said["correction_withdrawn"] is True, said

        again = json.loads(client.post(f"/api/expenses/{revised['fact']}/delete/").content)
        assert again["state"] == "already", (
            f"повторное удаление снова отвечает как первое: {again}"
        )
        assert again["correction_withdrawn"] is False, again
    finally:
        client.post("/logout/")

    assert live_total(sql, tenant) == before - Decimal("700.00")
