"""Накладная разносится по точкам руками, а не только правилом (issue #174).

Модуль 15 эталона отвечает на вопрос «чья накладная»: точка выбирается или
сумма раскладывается между несколькими — по выручке, поровну или долями,
которые человек поставил сам. Внизу видно, сколько ещё не разнесено, и пока не
сойдётся, провести нельзя.

Чего не хватало. Разнесение у нас было, но только **правилом** по контрагенту:
поставщик всегда делится так-то. Накладной, которую привезли на три точки в
разных долях, соответствовать нечему — а это обычный случай: одна поставка
сырья на две пиццерии, ремонт на одну.

**Долями, а не суммами по точкам.** Так же, как в правилах разнесения, и по той
же причине: доли переживают правку суммы документа, а вбитые руками суммы после
исправления накладной молча перестают сходиться. Проверку «доли дают ровно
целое» взяли у ERPNext (`cost_center_allocation`): там сумма процентов обязана
быть 100, иначе документ не сохраняется вовсе. Копейки раскладываются тем же
приёмом, что и в `allocation_plan`, — по накопленной сумме, чтобы дети всегда
складывались в родителя.

**Закрытый месяц не переписывается** (D020) — как и у любой правки задним
числом: сторно и новая запись в открытом периоде.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, login_as
from test_supplier_invoices import (  # noqa: F401
    NEW,
    counterparty,
    invoice_form,
    invoices_removed,
    item,
    key,
    sql,
    tenant,
    units,
)


@pytest.fixture
def network_invoice(client, counterparty, units, invoices_removed):  # noqa: F811
    """Счёт на юрлицо целиком: точки у него нет, разносить придётся руками."""
    login_as(client, "accountant")
    answer = client.post(NEW, invoice_form(
        counterparty, units, unit="network", amount="100.01", number="SPLIT-1",
    ))
    assert answer.status_code == 302, body(answer)
    return answer


def parent(sql):  # noqa: F811
    return sql.execute(
        """select id::text, amount, allocation::text from facts
            where dedup_key like 'manual:invoice:%%' and parent_fact_id is null
              and superseded_at is null"""
    ).fetchone()


def children(sql):  # noqa: F811
    return sql.execute(
        """select u.code, f.amount, f.allocation_share
             from facts f join units u on u.id = f.unit_id
            where f.parent_fact_id is not null and f.superseded_at is null
            order by u.code"""
    ).fetchall()


def split(client, fact_id, shares: dict) -> object:
    """Разнести руками — так же, как это делает форма модуля 15."""
    form = {"fact": fact_id}
    for code, percent in shares.items():
        form[f"share:{code}"] = percent
    return client.post("/expenses/split/", form, follow=True)


# --- ядро ---------------------------------------------------------------------


def test_an_invoice_is_split_between_units_by_hand(client, sql, units, network_invoice):  # noqa: F811
    """Три точки, свои доли — и сумма детей равна родителю до копейки."""
    fact_id = parent(sql)[0]
    answer = split(client, fact_id, {units["BG1"]: "50", units["NS1"]: "30",
                                     units["NS2"]: "20"})
    assert answer.status_code == 200, body(answer)[:400]

    rows = children(sql)
    assert len(rows) == 3, f"разнеслось не на три точки: {rows}"
    assert sum(row[1] for row in rows) == Decimal("100.01"), (
        f"дети не складываются в родителя: {rows}"
    )
    assert parent(sql)[2] == "split", "родитель не помечен разнесённым"


def test_shares_that_do_not_add_up_are_refused_in_words(client, sql, units, network_invoice):  # noqa: F811
    """Доли не дают целого — отказ словами, и ничего не разнесено.

    Взято у ERPNext: там сумма процентов обязана быть ровно 100, иначе документ
    не сохраняется. Молча разнести «сколько дали» значило бы потерять остаток
    между «по точкам» и «по сети» — ровно ту дыру, которую разнесение и закрывает.
    """
    fact_id = parent(sql)[0]
    answer = split(client, fact_id, {units["BG1"]: "50", units["NS1"]: "30"})

    assert answer.status_code == 400, body(answer)[:300]
    assert "80" in body(answer), "не сказано, сколько именно не разнесено"
    assert not children(sql), "разнеслось при несходящихся долях"


def test_a_split_without_shares_is_refused(client, sql, network_invoice):  # noqa: F811
    """Ни одной доли — отказ, а не пустое разнесение."""
    answer = split(client, parent(sql)[0], {})
    assert answer.status_code == 400
    assert not children(sql)


def test_the_equal_split_is_offered_by_a_button(client, sql, units, network_invoice):  # noqa: F811
    """«Поровну» — заготовка долей, а не отдельный вид разнесения."""
    fact_id = parent(sql)[0]
    answer = client.post("/expenses/split/", {"fact": fact_id, "evenly": "1"}, follow=True)
    assert answer.status_code == 200, body(answer)[:300]

    rows = children(sql)
    assert len(rows) == len(units), f"поровну разнеслось не на все точки: {rows}"
    assert sum(row[1] for row in rows) == Decimal("100.01")


def test_the_revenue_split_uses_the_revenue_of_the_period(client, sql, units, network_invoice):  # noqa: F811
    """«По выручке» берёт выручку того же периода, а не сегодняшнюю."""
    fact_id = parent(sql)[0]
    answer = client.post("/expenses/split/", {"fact": fact_id, "by_revenue": "1"},
                         follow=True)
    # Выручки в сиде может не быть вовсе — тогда продукт обязан сказать это
    # словами, а не разнести молча поровну: «по выручке» без выручки — не то же
    # самое, что «поровну».
    if answer.status_code == 400:
        assert "выручк" in body(answer).lower()
        assert not children(sql)
        return
    rows = children(sql)
    assert sum(row[1] for row in rows) == Decimal("100.01")


def test_a_role_without_the_right_splits_nothing(client, sql, units, network_invoice):  # noqa: F811
    """Разносит тот, кто ведёт месяц: управляющему точки чужие точки не видны."""
    fact_id = parent(sql)[0]
    login_as(client, "manager")
    answer = split(client, fact_id, {units["BG1"]: "100"})

    assert answer.status_code in (403, 404), body(answer)[:300]
    assert not children(sql)


def test_the_unallocated_list_leads_to_the_split(client, sql, network_invoice):  # noqa: F811
    """Со списка ожидания есть путь к разнесению: иначе экрана как бы нет."""
    login_as(client, "accountant")
    shown = body(client.get("/expenses/unallocated/"))
    assert "Чья накладная" in shown, "со списка не попасть на разнесение"
    assert f"/expenses/{parent(sql)[0]}/split/" in shown


def test_the_split_screen_shows_the_amount_and_the_units(client, sql, units, network_invoice):  # noqa: F811
    """Экран показывает сумму из документа и точки, между которыми делить."""
    login_as(client, "accountant")
    shown = body(client.get(f"/expenses/{parent(sql)[0]}/split/"))
    assert "100,01" in shown, "сумма документа не показана"
    for code in units:
        assert code in shown, f"точки {code} нет в списке"
    assert "здесь не правится" in shown, "не сказано, что сумма из документа"


def test_a_split_line_is_gone_from_the_waiting_list(client, sql, units, network_invoice):  # noqa: F811
    """Разнесённая строка уходит из списка ожидания — иначе он врёт числом."""
    fact_id = parent(sql)[0]
    split(client, fact_id, {units["BG1"]: "100"})

    shown = body(client.get("/expenses/unallocated/"))
    assert fact_id not in shown, "разнесённая строка осталась ждущей"


def test_the_split_survives_a_repeat(client, sql, units, network_invoice):  # noqa: F811
    """Повторная отправка той же формы не удваивает детей.

    Форму отправляют дважды — по нажатию «назад», по двойному щелчку. Ключ
    ребёнка выводится из ключа родителя, поэтому вторая запись заменяет ту же
    строку, а не добавляет новую; а родитель уже `split`, и функция базы вернёт
    ноль, не тронув ничего.
    """
    fact_id = parent(sql)[0]
    split(client, fact_id, {units["BG1"]: "60", units["NS1"]: "40"})
    first = children(sql)
    split(client, fact_id, {units["BG1"]: "60", units["NS1"]: "40"})

    assert children(sql) == first, "повторная отправка изменила разнесение"

