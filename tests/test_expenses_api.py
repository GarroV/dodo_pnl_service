"""Расходы по HTTP: тот же путь, что у экрана (T112).

Эндпоинт не «ещё одна дверь в данные», а вторая ручка той же двери. Отсюда
способ проверки: **каждый сценарий гоняется дважды — экраном и запросом, — и
результаты сравниваются**. Проверка «эндпоинт вернул 200» не проверяет ничего:
она зелена и у обёртки, которая отдаёт больше, чем экран.

Пять условий записаны в `docs/forge/spec.md`, секция «API и будущая
MCP-обёртка», и каждое проверяется отдельно.

**1. Роль и тенант приезжают контекстом базы.** В запросе их нет и быть не
может: срез делают те же политики, что рисуют страницу. Проверяется сравнением
построчно — управляющий получает ровно свои строки и ровно тот же итог, что
видит на экране, а не «что-то непустое».

**2. Срез по регистру — параметр запроса.** `?ledger=` сужает и никогда не
расширяет. Регистр, которого роль не видит, отвечает **тем же самым**, чем
отвечает выдуманное слово: иначе перебор значений через обёртку становится
способом узнать, какие регистры вообще есть.

**3. Записывающие вызовы только POST.** GET на запись — 405, и записи от него
не происходит. Причина уезжает в саму строку тем же полем, что у формы
(комментарий), а автор проставляется базой (`created_by = app_user_id()`).

**4. Отказ по невидимому неотличим от несуществующего.** Чужая точка, чужая
статья, чужой расход и выдуманный номер отвечают **побайтово одинаково**
(D014, D023). Это то место, на котором продукт спотыкался восемь раз, поэтому
сравнение здесь буквальное, а не «оба пустые».

**5. Долгих операций у расходов нет.** Список ограничен и говорит, что есть
ещё; пересчёт разнесения принимает **один** месяц за вызов и не обходит все
подряд, держа соединение.
"""
from __future__ import annotations

import json
import uuid
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
from test_expenses_list import WIDE, august_day, fact_id_of, record, shown, total_of

API = "/api/expenses/"
SCREEN = "/expenses/"
NEW = "/expenses/new/"


def answer(response) -> dict:
    """Разобранный ответ эндпоинта. Не JSON — сразу видно, что именно пришло."""
    text = response.content.decode()
    try:
        return json.loads(text)
    except json.JSONDecodeError:  # pragma: no cover — только при поломке
        raise AssertionError(
            f"ответ не JSON ({response.status_code}):\n{text[:2000]}"
        ) from None


def api_rows(client, **params) -> list[dict]:
    response = client.get(API, {**WIDE, **params})
    assert response.status_code == 200, body(response)
    return answer(response)["rows"]


def screen_rows(client, **params) -> list[dict]:
    return shown(body(client.get(SCREEN, {**WIDE, **params})))


@pytest.fixture
def other_item(sql, tenant):  # noqa: F811
    """Вторая статья расходов: нужна там, где проверяется отбор по статье.

    Заводится тем же способом, что первая (справочник поставляется пустым,
    Q015), и убирается за собой вместе со своими фактами.
    """
    from psycopg.types.json import Jsonb

    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    item_id = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'cash-test-2', %s, %s, '2020-01-01') returning id""",
        (tenant, Jsonb({"ru": "Бензин", "en": "Fuel", "sr-latn": "Gorivo"}), line),
    ).fetchone()[0]
    yield str(item_id)
    sql.execute("delete from facts where expense_item_id = %s", (item_id,))
    sql.execute("delete from expense_items where id = %s", (item_id,))


@pytest.fixture
def three_expenses(client, sql, item, units, facts_removed):  # noqa: F811
    """Три расхода на трёх точках, внесённые с экрана бухгалтером.

    Материал общий для сравнений: с экрана, а не прямым `insert`, — сравнивать
    экран с запросом имеет смысл только на строках, которые продукт записал
    своим обычным путём.
    """
    login_as(client, "accountant")
    keys = {
        code: record(client, item, units, unit=units[code], amount=amount)
        for code, amount in (("NS1", "100.00"), ("NS2", "200.00"), ("BG1", "300.00"))
    }
    client.post("/logout/")
    return keys


# --- условие 1: срез роли и тенанта приезжают контекстом базы ------------------


@pytest.mark.parametrize("role", ["manager", "accountant", "director"])
def test_the_api_gives_each_role_exactly_what_the_screen_gives(
    client, three_expenses, role,
):
    """Построчно и до копейки: запрос и экран — один и тот же срез.

    Сломайте `unit_visibility` на `facts` — тест покраснеет у управляющего
    сразу двумя способами: в ответе появятся чужие строки, и разойдётся итог.
    """
    login_as(client, role)

    from_screen = screen_rows(client)
    response = client.get(API, WIDE)
    assert response.status_code == 200, body(response)
    got = answer(response)

    assert [row["id"] for row in got["rows"]] == [row["id"] for row in from_screen], (
        f"роль {role}: запрос и экран показывают разные строки"
    )
    assert [row["amount"] for row in got["rows"]] == [
        str(row["amount"]) for row in from_screen
    ]
    assert Decimal(got["total"]) == total_of(body(client.get(SCREEN, WIDE))), (
        f"роль {role}: итог запроса разошёлся с итогом экрана"
    )


def test_the_manager_gets_neither_the_rows_of_other_units_nor_their_codes(
    client, three_expenses, units,  # noqa: F811
):
    """Чужая точка не приходит ни строкой, ни кодом внутри ответа."""
    login_as(client, "manager")
    response = client.get(API, WIDE)
    text = response.content.decode()

    assert "BG1" not in text and "NS2" not in text, text
    assert [row["unit"] for row in answer(response)["rows"]] == ["NS1"]


def test_nobody_gets_in_without_signing_in(client, three_expenses):
    """Не вошёл — 401 ответом эндпоинта, а не страницей входа.

    Перенаправление на форму входа обёртка разобрать не может и покажет модели
    HTML страницы входа как «данные».
    """
    client.post("/logout/")
    response = client.get(API, WIDE)
    assert response.status_code == 401, body(response)
    assert answer(response)["error"]


# --- условие 2: срез по регистру параметром запроса ----------------------------


def test_the_ledger_cut_narrows_and_never_widens(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """`?ledger=` сужает список; сумма среза не больше суммы без среза."""
    login_as(client, "director")
    record(client, item, units, unit=units["NS1"], amount="50.00", ledger="official")
    record(client, item, units, unit=units["NS1"], amount="70.00", ledger="internal")

    whole = api_rows(client)
    official = api_rows(client, ledger="official")
    internal = api_rows(client, ledger="internal")

    assert {row["id"] for row in official} | {row["id"] for row in internal} <= {
        row["id"] for row in whole
    }, "срез показал строку, которой нет в полном списке"
    assert all(row["ledger_code"] == "official" for row in official), official
    assert len(official) + len(internal) <= len(whole)


def test_an_invisible_ledger_answers_exactly_like_a_made_up_word(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Невидимый регистр и выдуманное слово — один ответ побайтово (D023).

    Разные ответы означали бы, что перебором значений в адресе составляется
    список регистров партнёра, не увидев ни одной строки.
    """
    login_as(client, "director")
    record(client, item, units, unit=units["NS1"], amount="70.00", ledger="internal")
    client.post("/logout/")

    # Управляющему внутренний регистр не виден вовсе (official + supplementary).
    login_as(client, "manager")
    hidden = client.get(API, {**WIDE, "ledger": "internal"})
    invented = client.get(API, {**WIDE, "ledger": "no-such-ledger"})

    assert hidden.status_code == invented.status_code == 200
    assert hidden.content == invented.content, (
        f"невидимый регистр отличим от выдуманного:\n{hidden.content}\n{invented.content}"
    )
    assert answer(hidden)["rows"] == []


# --- условие 3: записывающие вызовы только POST --------------------------------


@pytest.mark.parametrize("path", ["delete", "allocate"])
def test_writing_calls_refuse_get(client, three_expenses, sql, path):  # noqa: F811
    """Запись по ссылке из истории браузера случиться не должна."""
    login_as(client, "director")
    key = next(iter(three_expenses.values()))
    url = (
        f"{API}{fact_id_of(sql, key)}/delete/" if path == "delete" else f"{API}allocate/"
    )

    response = client.get(url)
    assert response.status_code == 405, body(response)
    assert "POST" in response.headers.get("Allow", "")
    # И записи от этого не произошло: строка на месте и действует.
    assert facts_of(sql, key)[0][9] is None, "GET удалил расход"


def test_the_reason_travels_into_the_record(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Причина уезжает в саму строку, а автора проставляет база."""
    login_as(client, "director")
    key = entry_key()
    response = client.post(API, {
        **payload(item, units, entry_key=key, date=august_day()),
        "unit": units["NS1"],
        "note": "бензин курьеру",
    })
    assert response.status_code == 200, body(response)

    written = sql.execute(
        "select note, created_by from facts where dedup_key = %s",
        (f"manual:cash:{key}",),
    ).fetchone()
    assert written[0] == "бензин курьеру"
    assert written[1] is not None, "у записи нет автора"


# --- условие 4: отказ по невидимому неотличим от несуществующего ---------------


def test_a_hand_picked_unit_gives_neither_rows_nor_a_hint(
    client, three_expenses, units,  # noqa: F811
):
    """Чужая точка в отборе отвечает так же, как выдуманный номер."""
    login_as(client, "manager")
    foreign = client.get(API, {**WIDE, "unit": units["BG1"]})
    invented = client.get(API, {**WIDE, "unit": str(uuid.uuid4())})

    assert foreign.status_code == invented.status_code == 200
    assert foreign.content == invented.content, (
        f"чужая точка отличима от выдуманной:\n{foreign.content}\n{invented.content}"
    )
    assert answer(foreign)["rows"] == []


def test_a_real_item_with_nothing_visible_answers_like_a_made_up_one(
    client, sql, item, other_item, units, facts_removed,  # noqa: F811
):
    """Статья существует и видна в справочнике, а её расход — на чужой точке.

    Управляющий обязан получить то же самое, что на выдуманный номер: по ответу
    нельзя понять, есть ли по этой статье расходы у соседей.
    """
    login_as(client, "accountant")
    record(client, other_item, units, unit=units["BG1"], amount="42.00")
    client.post("/logout/")

    login_as(client, "manager")
    real = client.get(API, {**WIDE, "item": other_item})
    invented = client.get(API, {**WIDE, "item": str(uuid.uuid4())})

    assert real.status_code == invented.status_code == 200
    assert real.content == invented.content, (
        f"настоящая статья отличима от выдуманной:\n{real.content}\n{invented.content}"
    )
    assert answer(real)["rows"] == []


def test_a_foreign_expense_answers_like_a_made_up_number(
    client, three_expenses, sql,  # noqa: F811
):
    """Карточка чужого расхода и карточка выдуманного номера — один ответ."""
    login_as(client, "manager")
    foreign = client.get(f"{API}{fact_id_of(sql, three_expenses['BG1'])}/")
    invented = client.get(f"{API}{uuid.uuid4()}/")

    assert foreign.status_code == invented.status_code == 404
    assert foreign.content == invented.content, (
        f"чужой расход отличим от выдуманного:\n{foreign.content}\n{invented.content}"
    )


def test_writing_to_a_foreign_unit_is_refused_in_the_same_words(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Подмена точки в запросе: чужая, чужого партнёра и выдуманная — один отказ."""
    login_as(client, "manager")
    keys = [entry_key(), entry_key()]
    refusals = [
        client.post(API, {
            **payload(item, units, entry_key=key, date=august_day()),
            "unit": unit,
        })
        for key, unit in zip(keys, (units["BG1"], str(uuid.uuid4())), strict=True)
    ]
    assert {response.status_code for response in refusals} == {400}, [
        body(response) for response in refusals
    ]
    assert refusals[0].content == refusals[1].content, [
        response.content for response in refusals
    ]
    for key in keys:
        assert not facts_of(sql, key), "запись по подобранной точке всё-таки прошла"


# --- сравнение сценариев: экран и запрос дают одно и то же ---------------------


def test_recording_through_the_api_lands_exactly_like_the_form(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Одни и те же поля формой и запросом дают одинаковую строку."""
    login_as(client, "director")
    by_form = entry_key()
    by_api = entry_key()
    fields = {"amount": "321.00", "unit": units["NS1"], "date": august_day()}

    assert client.post(NEW, payload(item, units, entry_key=by_form, **fields)).status_code == 302
    assert client.post(API, payload(item, units, entry_key=by_api, **fields)).status_code == 200

    def shape(key):
        row = facts_of(sql, key)[0]
        return row[:9]  # точка, сумма, регистр, канал, источник, период, дата, разнесение, версия

    assert shape(by_form) == shape(by_api)


def test_the_api_accepts_json_the_same_way_it_accepts_a_form(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Тело JSON разбирается тем же разбором, что форма: путь записи один."""
    login_as(client, "director")
    key = entry_key()
    response = client.post(
        API,
        json.dumps(payload(item, units, entry_key=key, date=august_day(),
                           unit=units["NS1"], amount="12.34")),
        content_type="application/json",
    )
    assert response.status_code == 200, body(response)
    assert str(facts_of(sql, key)[0][1]) == "12.34"


def test_a_closed_month_lands_in_the_current_one_through_the_api_too(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Расход июньской датой после закрытия июня ложится в текущий месяц.

    Ровно то же, что делает форма (T109): закрытый месяц не сдвигается ни на
    копейку, а исходная дата остаётся при строке.
    """
    login_as(client, "manager")
    assert client.post(NEW, payload(item, units, entry_key=entry_key())).status_code == 302
    client.post("/logout/")
    before = june_total(sql)
    assert before > 0, "нечему двигаться: в июне нет ни одного расхода"

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "director")
    key = entry_key()
    response = client.post(API, {
        **payload(item, units, entry_key=key, date=JUNE_DAY), "unit": units["NS1"],
    })
    assert response.status_code == 200, body(response)
    got = answer(response)
    assert got["period"] == f"{current_period():%Y-%m}"
    assert got["moved_from"] == "2026-06"

    row = facts_of(sql, key)[0]
    assert row[5] == current_period() and str(row[6]) == JUNE_DAY
    assert june_total(sql) == before, "закрытый месяц сдвинулся"


def test_editing_and_removing_through_the_api_match_the_screen(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Правка заменяет версию, удаление помечает — как на экране (T110)."""
    login_as(client, "director")
    key = record(client, item, units, unit=units["NS1"], amount="100.00")
    fact_id = fact_id_of(sql, key)

    edited = client.post(f"{API}{fact_id}/", {
        **payload(item, units, entry_key=key, date=august_day()),
        "unit": units["NS1"], "amount": "150.00",
    })
    assert edited.status_code == 200, body(edited)
    versions = facts_of(sql, key)
    assert len(versions) == 2, versions
    assert [str(row[1]) for row in versions] == ["100.00", "150.00"]
    assert versions[0][9] is not None and versions[0][10] == versions[1][12]

    removed = client.post(f"{API}{fact_id_of(sql, key)}/delete/")
    assert removed.status_code == 200, body(removed)
    assert not [row for row in facts_of(sql, key) if row[9] is None], (
        "удалённый расход остался действующим"
    )
    # И он по-прежнему виден списком — с состоянием, а не бесследно исчез.
    assert any(row["state"] == "removed" for row in api_rows(client))


# --- условие 5: долгих операций нет -------------------------------------------


def test_the_list_is_capped_and_says_there_is_more(client, three_expenses):
    """Список не отдаёт неограниченную выборку и сам говорит, что есть ещё."""
    login_as(client, "director")
    page = answer(client.get(API, {**WIDE, "limit": "1"}))
    assert len(page["rows"]) == 1
    assert page["has_more"] is True
    assert Decimal(page["total"]) == Decimal(page["rows"][0]["amount"]), (
        "итог обязан быть суммой показанных строк, а не всей выборки"
    )

    rest = answer(client.get(API, {**WIDE, "limit": "1", "offset": "1"}))
    assert rest["rows"][0]["id"] != page["rows"][0]["id"]


def test_reallocation_takes_one_month_and_refuses_to_walk_them_all(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Пересчёт разнесения принимает месяц, а не «все»: соединение не держится.

    Кнопка на экране обходит месяцы сама, и это её право — она отвечает
    человеку, который смотрит на страницу. Вызову соединение держать нельзя, и
    вместо неограниченного обхода он просит назвать месяц.
    """
    login_as(client, "director")
    record(client, item, units, unit=units["NS1"], amount="10.00")

    without = client.post(f"{API}allocate/")
    assert without.status_code == 400, body(without)
    assert answer(without)["error"]

    done = client.post(f"{API}allocate/", {"period": f"{current_period():%Y-%m}"})
    assert done.status_code == 200, body(done)
    assert answer(done)["changed"] == 0, "менять было нечего, а пересчёт что-то написал"


def test_the_unallocated_list_is_the_same_through_the_api(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Нераспределённое видно и запросом: сумма без точки не теряется молча."""
    login_as(client, "director")
    key = entry_key()
    assert client.post(NEW, {
        **payload(item, units, entry_key=key, date=august_day()), "unit": "network",
    }).status_code == 302

    from_api = answer(client.get(f"{API}unallocated/"))
    ours = [row for row in from_api["rows"] if row["id"] == fact_id_of(sql, key)]
    assert ours, from_api
    assert Decimal(ours[0]["amount"]) == Decimal("1200.50")
    # Итог — сумма показанных строк, как и в списке расходов: второй выборкой он
    # разошёлся бы с таблицей молча.
    assert Decimal(from_api["total"]) == sum(
        Decimal(row["amount"]) for row in from_api["rows"]
    )
