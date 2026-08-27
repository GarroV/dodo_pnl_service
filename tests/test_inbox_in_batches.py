"""Инбокс разбирается пачкой, и решение запоминается (issue #173).

Эталон (модуль 3) ставит цель числом: «разобрать сорок штук за десять минут —
это цель, а не метафора». Достигается она двумя вещами, и обе проверяются здесь:

* **пачкой** — отметил несколько строк одного поставщика и присвоил статью всем
  сразу, одним действием вместо сорока;
* **памятью** — продукт запоминает, какую статью человек дал этому поставщику, и
  в следующий раз предлагает её сам.

Память намеренно **предлагает, а не проставляет**. Автоматическая простановка —
это уже распознавание, у неё другая цена ошибки: назначенная молча статья
уезжает в P&L, и находят её на сверке. Предложение видно и отклоняется одним
движением, поэтому оно и проверяется отдельным тестом.

Проверки идут через экраны, как в `test_supplier_inbox.py`: разбор — это запись
денег, и проверять его мимо формы значило бы проверять не то, чем пользуются.
"""
from __future__ import annotations

import pytest

from conftest import MONTH_CYCLE_WITHOUT_CLASSIFY, body, login_as, narrowed_permissions
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

INBOX = "/inbox/"
BATCH = "/inbox/classify/"


@pytest.fixture
def second_item(sql, tenant):  # noqa: F811
    """Вторая статья: ею переучивают память.

    Просить её нужно **раньше** строк инбокса — фикстуры разбираются в обратном
    порядке, а статью, на которую ссылаются строки, база удалить не даст, пока
    строки живы.
    """
    from psycopg.types.json import Jsonb

    pnl = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    row = str(sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'inv-test-2', %s, %s, '2020-01-01') returning id""",
        (tenant, Jsonb({"ru": "Вывоз мусора", "en": "Waste", "sr-latn": "Smeće"}), pnl),
    ).fetchone()[0])
    yield row
    sql.execute("delete from expense_items where id = %s", (row,))


@pytest.fixture
def rules_removed(sql):  # noqa: F811
    """Память разбора не переживает тест.

    Просить её нужно **после** `item`: фикстуры разбираются в обратном порядке,
    а статью, на которую ссылается запомненное решение, база удалить не даст.
    Каскад у этой связи в Django, а не в схеме, и прямой SQL до него не
    дотягивается — ровно тот случай, ради которого уборка пишется явно.
    """
    yield
    sql.execute("delete from classification_rules")


@pytest.fixture
def three_lines(client, counterparty, units, invoices_removed, rules_removed):  # noqa: F811
    """Три счёта одного поставщика без статьи: типичное утро в инбоксе."""
    login_as(client, "accountant")
    made = []
    for number in ("EPS-1", "EPS-2", "EPS-3"):
        answer = client.post(NEW, invoice_form(
            counterparty, units, item="", number=number, amount="1000.00",
        ))
        assert answer.status_code == 302, body(answer)
        made.append(number)
    return made


def waiting_ids(client) -> list[str]:
    """Номера строк, стоящих в инбоксе, — так же, как их берёт браузер."""
    import re

    return re.findall(r'data-fact="([0-9a-f-]{36})"', body(client.get(INBOX)))


def sorted_out(sql, item: str) -> int:  # noqa: F811
    return sql.execute(
        "select count(*) from facts where expense_item_id = %s and superseded_at is null",
        (item,),
    ).fetchone()[0]


# --- пачкой -------------------------------------------------------------------


def test_several_lines_are_sorted_in_one_action(client, sql, item, three_lines):  # noqa: F811
    """Отметил три строки — присвоил статью всем сразу, одним запросом."""
    ids = waiting_ids(client)
    assert len(ids) == 3, "инбокс собрался не тем, чем ожидалось"

    answer = client.post(BATCH, {"facts": ids, "item": item, "unit": "network"})
    assert answer.status_code == 302, body(answer)[:400]
    assert sorted_out(sql, item) == 3, "пачкой разобрались не все строки"
    assert not waiting_ids(client), "разобранные строки остались в инбоксе"


def test_the_batch_says_how_many_it_sorted(client, item, three_lines):  # noqa: F811
    """Сколько строк разобралось, сказано словами: молчание читается как сбой."""
    ids = waiting_ids(client)
    shown = body(client.post(BATCH, {"facts": ids, "item": item, "unit": "network"},
                             follow=True))
    assert "Разобрано строк: 3" in shown


def test_an_empty_batch_is_refused_in_words(client, sql, item, three_lines):  # noqa: F811
    """Ничего не отмечено — отказ словами, а не молчаливый успех."""
    answer = client.post(BATCH, {"facts": [], "item": item, "unit": "network"})
    assert answer.status_code == 400
    assert "Отметьте строки" in body(answer)
    assert sorted_out(sql, item) == 0


def test_a_refused_batch_sorts_nothing(client, sql, item, three_lines):  # noqa: F811
    """Отказ по одной строке возвращает всю пачку: половинчатого разбора нет.

    Статья, не действовавшая в период строк, — как раз такой случай: продукт
    отвечает отказом, а не разбирает «те, что подошли».
    """
    ids = waiting_ids(client)
    sql.execute("update expense_items set valid_from = '2030-01-01' where id = %s", (item,))

    answer = client.post(BATCH, {"facts": ids, "item": item, "unit": "network"})
    assert answer.status_code == 400, body(answer)[:300]
    assert sorted_out(sql, item) == 0
    assert len(waiting_ids(client)) == 3, "строки ушли из инбокса при отказе"


def test_a_row_that_is_not_ours_is_not_sorted(client, sql, item, three_lines):  # noqa: F811
    """Управляющему точки строки на сеть не видны — и разобрать он их не может.

    Ответ тот же, что на чужую строку и на выдуманный номер (D023): по нему
    нельзя понять, что строка существует. Отдельного «403» здесь нет, потому что
    срез делает база, а не проверка роли в представлении.
    """
    ids = waiting_ids(client)
    login_as(client, "manager")

    answer = client.post(BATCH, {"facts": ids, "item": item, "unit": "network"})
    assert answer.status_code == 404, body(answer)[:300]
    assert sorted_out(sql, item) == 0


# --- память -------------------------------------------------------------------


def test_the_decision_is_remembered_and_offered_next_time(
    client, sql, item, three_lines,  # noqa: F811
):
    """Разобрал строку — следующая строка того же поставщика приходит с подсказкой."""
    first = waiting_ids(client)[0]
    answer = client.post(f"/inbox/{first}/classify/",
                         {"item": item, "unit": "network"})
    assert answer.status_code == 302, body(answer)[:300]

    remembered = sql.execute(
        "select count(*) from classification_rules where expense_item_id = %s", (item,)
    ).fetchone()[0]
    assert remembered == 1, "решение не запомнилось"

    shown = body(client.get(INBOX))
    assert "Похоже на" in shown
    assert shown.count('class="chip">Электричество') == 2, (
        "подсказка не показана оставшимся строкам того же поставщика"
    )


def test_the_suggestion_does_not_sort_anything_by_itself(
    client, sql, item, three_lines,  # noqa: F811
):
    """Предложение остаётся предложением: строка ждёт человека.

    Автоматическая простановка — это уже распознавание: назначенная молча
    статья уезжает в P&L, и находят её на сверке.
    """
    first = waiting_ids(client)[0]
    client.post(f"/inbox/{first}/classify/", {"item": item, "unit": "network"})

    assert sorted_out(sql, item) == 1, "подсказка разобрала строки сама"
    assert len(waiting_ids(client)) == 2


def test_a_new_decision_replaces_the_old_one(client, sql, second_item, item, three_lines):  # noqa: F811
    """Переучили — предлагается новое: память не спорит с последним решением."""
    ids = waiting_ids(client)
    client.post(f"/inbox/{ids[0]}/classify/", {"item": item, "unit": "network"})
    client.post(f"/inbox/{ids[1]}/classify/", {"item": second_item, "unit": "network"})

    rule = sql.execute(
        "select expense_item_id::text, hits from classification_rules"
    ).fetchall()
    assert rule == [(second_item, 1)], "память осталась на прежней статье"
    assert "Вывоз мусора" in body(client.get(INBOX))


def test_repeating_the_same_decision_counts_it(client, sql, item, three_lines):  # noqa: F811
    """Подтверждённое дважды соответствие видно счётчиком, а не одинаково с разовым."""
    ids = waiting_ids(client)
    client.post(f"/inbox/{ids[0]}/classify/", {"item": item, "unit": "network"})
    client.post(f"/inbox/{ids[1]}/classify/", {"item": item, "unit": "network"})

    hits = sql.execute("select hits from classification_rules").fetchone()[0]
    assert hits == 2


def test_a_role_without_the_right_to_remember_still_sorts(
    client, sql, web_env, item, three_lines,  # noqa: F811
):
    """Памяти нет права — разбор всё равно проходит, а не падает пятисоткой.

    Политика `classify_insert` требует `suppliers.classify`. Роль, которая
    строки разбирать может, а память вести — нет, получила бы отказ базы
    посреди запроса, и при `ATOMIC_REQUESTS` вместе с памятью развалился бы сам
    разбор. Это не гипотеза: ровно такая роль стояла на стенде — у бухгалтера
    права не было, форма ролей туда не доехала.
    """
    first = waiting_ids(client)[0]
    with narrowed_permissions(web_env, "accountant", MONTH_CYCLE_WITHOUT_CLASSIFY):
        answer = client.post(f"/inbox/{first}/classify/",
                             {"item": item, "unit": "network"})
        assert answer.status_code == 302, body(answer)[:300]

    assert sorted_out(sql, item) == 1, "строка не разобралась"
    remembered = sql.execute("select count(*) from classification_rules").fetchone()[0]
    assert remembered == 0, "память записалась без права на неё"
