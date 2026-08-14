"""Внесение расхода из кассы (T109).

Три правила, ради которых написан экран, и каждое проверяется отдельно.

**1. Точку отвергает база, а не форма.** Управляющему точка подставляется своя,
но проверяется это **подменой**: в POST кладётся чужая точка, и отказ обязан
прийти от политик (`unit_visibility` на `facts`), а не от списка вариантов в
представлении. Забытый фильтр должен давать отказ, а не чужую строку (D014).
Поэтому представление точку **не фильтрует**: оно передаёт её базе как есть.

**2. Закрытый месяц не двигается ни на копейку.** Расход, датированный внутри
утверждённого месяца, ложится в текущий открытый период с исходной датой
документа, а не в закрытый (D020: разница переносится в текущий период,
переписывать закрытый молча нельзя). Итог закрытого месяца до и после обязан
совпасть.

**3. Отказ не рассказывает лишнего.** Ответ на чужую точку неотличим от ответа
на несуществующую (D023, D014): по коду и тексту нельзя понять, что точка
вообще есть.

Проверки идут **через экран**, то есть тем же путём, что и человек. Прямое
чтение базы здесь только для того, чтобы посмотреть, что записалось, — и идёт
владельцем схемы, потому что это не проверка доступа, а осмотр результата.
"""
from __future__ import annotations

import re
import uuid
from datetime import date

import pytest

from conftest import body, login_as
from test_directory import approve_june, payruns_restored, sql  # noqa: F401

URL = "/expenses/new/"
JUNE_DAY = "2026-06-15"

# Месяц, в который продукт кладёт расход за закрытый месяц: текущий. Считается
# от сегодняшнего дня, а не приколочен строкой, — иначе тест сломался бы в
# первый день следующего месяца, ничего не найдя в продукте.
def current_period() -> date:
    return date.today().replace(day=1)


@pytest.fixture
def tenant(sql):  # noqa: F811
    return str(sql.execute("select id from tenants where code = 'rs-dev'").fetchone()[0])


@pytest.fixture
def units(sql, tenant):  # noqa: F811
    return {
        code: str(unit_id)
        for unit_id, code in sql.execute(
            "select id, code from units where tenant_id = %s", (tenant,)
        ).fetchall()
    }


@pytest.fixture
def item(sql, tenant):  # noqa: F811
    """Статья расходов для внесения. Кладётся владельцем схемы: это подготовка.

    Справочник статей поставляется пустым намеренно (Q015), поэтому материал
    для теста заводится тестом, а не берётся из сида.
    """
    from psycopg.types.json import Jsonb

    line = sql.execute("select id from pnl_items where code = 'food_cost'").fetchone()[0]
    item_id = sql.execute(
        """insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
           values (%s, 'cash-test', %s, %s, '2020-01-01') returning id""",
        (tenant, Jsonb({"ru": "Вода", "en": "Water", "sr-latn": "Voda"}), line),
    ).fetchone()[0]
    yield str(item_id)
    sql.execute("delete from facts where expense_item_id = %s", (item_id,))
    sql.execute("delete from expense_items where id = %s", (item_id,))


@pytest.fixture
def facts_removed(sql, tenant):  # noqa: F811
    """Расходы, внесённые тестом, не переживают его.

    Просить эту фикстуру нужно **раньше** `payruns_restored`: разбираются они в
    обратном порядке, а факт закрытого месяца база удалить не даст
    (`facts_guard`), пока месяц не открыт заново.

    Вместе с расходами убираются и месяцы, которые они за собой завели (T135):
    запись факта заводит недостающий период сама, и оставленный им месяц ехал бы
    дальше по прогону. Соседние тесты берут период «первой ссылкой со списка», а
    список отсортирован по убыванию месяца — то есть оставленный август молча
    подменял бы им июнь. Убираются только месяцы, появившиеся за время теста, и
    только пустые: строку с расчётом или фактом трогать нельзя.
    """
    before = [
        row[0] for row in sql.execute(
            "select period from periods where tenant_id = %s", (tenant,)
        ).fetchall()
    ]
    yield
    sql.execute("delete from facts where dedup_key like 'manual:cash:%%'")
    sql.execute(
        """delete from periods p
            where p.tenant_id = %s
              and p.period <> all(%s::date[])
              and not exists (
                  select 1 from facts f
                   where f.tenant_id = p.tenant_id and f.period = p.period)
              and not exists (
                  select 1 from payruns r
                   where r.tenant_id = p.tenant_id and r.period = p.period)""",
        (tenant, before),
    )


def entry_key() -> str:
    return str(uuid.uuid4())


def payload(item_id: str, units: dict, **extra) -> dict:
    fields = {
        "date": JUNE_DAY,
        "amount": "1200.50",
        "item": item_id,
        "note": "вода в BG1",
        "entry_key": entry_key(),
    }
    fields.update(extra)
    return fields


def facts_of(sql, key: str) -> list[tuple]:  # noqa: F811
    return sql.execute(
        """select unit_id::text, amount, ledger::text, channel::text, source::text,
                  period, doc_date, allocation::text, revision, superseded_at,
                  superseded_by::text, expense_item_id::text, id::text
             from facts where dedup_key = %s order by revision""",
        (f"manual:cash:{key}",),
    ).fetchall()


def june_total(sql) -> object:  # noqa: F811
    """Сумма действующих фактов закрытого месяца — то, что не должно двигаться."""
    return sql.execute(
        "select coalesce(sum(amount), 0) from facts "
        "where period = '2026-06-01' and superseded_at is null"
    ).fetchone()[0]


# --- внесение -----------------------------------------------------------------


def test_the_manager_records_an_expense_for_his_own_unit(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Управляющему точка подставляется своя: выбирать её ему не из чего."""
    login_as(client, "manager")
    try:
        page = body(client.get(URL))
        assert "NS1" in page, "форма не показала точку, на которую пойдёт расход"

        key = entry_key()
        answer = client.post(URL, payload(item, units, entry_key=key))
        assert answer.status_code == 302, body(answer)

        rows = facts_of(sql, key)
        assert len(rows) == 1, rows
        unit_id, amount, ledger, channel, source, period, doc_date, allocation, *_rest = rows[0]
        assert unit_id == units["NS1"]
        assert str(amount) == "1200.50"
        assert (ledger, channel, source, allocation) == (
            "official", "cash", "manual", "direct",
        )
        assert doc_date == date(2026, 6, 15)
        assert period == date(2026, 6, 1), "период взят не из даты расхода"
        assert rows[0][11] == item, "статья не записана в факт"
    finally:
        client.post("/logout/")


def test_the_stored_title_does_not_depend_on_the_page_language(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Название в самом факте одно и то же, кем бы расход ни вносили.

    Иначе одна статья попадала бы в данные то «Вода», то «Voda» — в зависимости
    от языка вносившего, — и в отчёте это выглядело бы двумя разными строками.
    Читателю название всё равно показывается на его языке: статья при факте есть.
    """
    login_as(client, "manager")
    client.cookies["django_language"] = "en"
    try:
        key = entry_key()
        assert client.post(URL, payload(item, units, entry_key=key)).status_code == 302
        stored = sql.execute(
            "select title from facts where dedup_key = %s", (f"manual:cash:{key}",)
        ).fetchone()[0]
        assert stored == "Вода", stored
    finally:
        client.cookies.pop("django_language", None)
        client.post("/logout/")


@pytest.mark.parametrize("role", ["accountant", "director"])
def test_the_accountant_and_the_director_record_for_any_unit(
    client, sql, item, units, facts_removed, role,  # noqa: F811
):
    """Бухгалтер и оперативный директор вносят по любой точке (D036)."""
    login_as(client, role)
    try:
        key = entry_key()
        answer = client.post(URL, payload(item, units, unit=units["BG1"], entry_key=key))
        assert answer.status_code == 302, body(answer)
        rows = facts_of(sql, key)
        assert len(rows) == 1 and rows[0][0] == units["BG1"], rows
    finally:
        client.post("/logout/")


def test_an_expense_without_an_item_is_refused(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Расход без статьи не сохраняется: иначе он не попадёт ни в одну строку P&L."""
    login_as(client, "manager")
    try:
        key = entry_key()
        answer = client.post(URL, payload(item, units, item="", entry_key=key))
        assert answer.status_code == 400, answer.status_code
        assert facts_of(sql, key) == []
    finally:
        client.post("/logout/")


def test_the_amount_must_be_a_positive_number(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    login_as(client, "manager")
    try:
        for wrong in ("", "0", "-5", "много"):
            key = entry_key()
            answer = client.post(URL, payload(item, units, amount=wrong, entry_key=key))
            assert answer.status_code == 400, f"сумма {wrong!r}: {answer.status_code}"
            assert facts_of(sql, key) == [], f"сумма {wrong!r} записалась"
    finally:
        client.post("/logout/")


# --- правило 1: точку отвергает база ------------------------------------------


def test_the_manager_cannot_substitute_another_unit(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Главная проверка задачи: подмена точки в запросе отвергается.

    Отвергать обязана база: представление точку не фильтрует, оно передаёт её
    как есть. Сломайте политику `unit_visibility` на `facts` — и этот тест
    покраснеет, потому что чужая строка запишется.
    """
    login_as(client, "manager")
    try:
        key = entry_key()
        answer = client.post(URL, payload(item, units, unit=units["BG1"], entry_key=key))
        assert answer.status_code == 400, answer.status_code
        assert facts_of(sql, key) == [], "расход лёг на чужую точку"
    finally:
        client.post("/logout/")


def test_the_refusal_does_not_reveal_that_the_unit_exists(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Чужая точка и несуществующая отвечают одинаково (D023, D014).

    Разный ответ означал бы, что перебором значений в форме можно составить
    список чужих точек, ни одной из них не увидев.
    """
    login_as(client, "manager")
    try:
        alien = client.post(URL, payload(item, units, unit=units["BG1"], entry_key=entry_key()))
        nobody = client.post(
            URL, payload(item, units, unit=str(uuid.uuid4()), entry_key=entry_key())
        )
        assert alien.status_code == nobody.status_code
        assert _message(body(alien)) == _message(body(nobody)), (
            "по тексту отказа видно, что чужая точка существует"
        )
    finally:
        client.post("/logout/")


def test_a_made_up_unit_is_refused_in_words_even_to_a_role_without_limits(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Выдуманный номер точки — отказ, а не 500.

    Директору точки не ограничены, и политика пропускает любой uuid: у того, кто
    видит все точки, невидимых нет. Отсекает такую строку внешний ключ, а он у
    Django отложенный — то есть проверяется на коммите, когда объяснять уже
    некому. Без явной проверки на месте человек получал бы оборванный запрос.
    """
    login_as(client, "director")
    try:
        key = entry_key()
        answer = client.post(
            URL, payload(item, units, unit=str(uuid.uuid4()), entry_key=key)
        )
        assert answer.status_code == 400, answer.status_code
        assert facts_of(sql, key) == []
    finally:
        client.post("/logout/")


def _message(html: str) -> str:
    """Текст плашки отказа — по нему и сравниваем два ответа."""
    found = re.search(r'class="alert"[^>]*>(.*?)</div>', html, re.S)
    return re.sub(r"\s+", " ", found.group(1)).strip() if found else ""


def test_an_invisible_ledger_cannot_be_chosen(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Регистр расхода есть (Q013, вариант Б), но только из видимых роли.

    Управляющий не видит внутреннего регистра (D031): предложить ему завести
    туда расход значило бы дать записать данные, которых он потом не найдёт.
    """
    login_as(client, "manager")
    try:
        page = body(client.get(URL))
        assert 'value="official"' in page and 'value="supplementary"' in page
        assert 'value="internal"' not in page, "форма предложила невидимый регистр"

        key = entry_key()
        answer = client.post(URL, payload(item, units, ledger="internal", entry_key=key))
        assert answer.status_code == 400, answer.status_code
        assert facts_of(sql, key) == []
    finally:
        client.post("/logout/")


# --- правило 2: закрытый месяц не двигается -----------------------------------


def test_an_expense_dated_in_a_closed_month_lands_beside_it(
    client, sql, web_env, item, units, facts_removed, payruns_restored,  # noqa: F811
):
    """Закрытый месяц не сдвигается ни на копейку, а расход не теряется.

    Сначала расход вносится в **открытый** июнь — чтобы у закрытого месяца было
    что двигать. Потом июнь утверждается, и тот же по дате расход вносится
    заново: он обязан лечь в текущий месяц с исходной датой документа.
    """
    login_as(client, "manager")
    first = entry_key()
    assert client.post(URL, payload(item, units, entry_key=first)).status_code == 302
    client.post("/logout/")

    before = june_total(sql)
    assert before > 0, "нечему двигаться: в июне нет ни одного расхода"

    approve_june(client, web_env)
    client.post("/logout/")

    login_as(client, "manager")
    try:
        second = entry_key()
        answer = client.post(URL, payload(item, units, amount="777.00", entry_key=second))
        assert answer.status_code == 302, body(answer)

        rows = facts_of(sql, second)
        assert len(rows) == 1, rows
        period, doc_date = rows[0][5], rows[0][6]
        assert doc_date == date(2026, 6, 15), "дата расхода потерялась"
        assert period == current_period(), (
            f"расход за закрытый месяц лёг в период {period}, а не в текущий"
        )
        assert june_total(sql) == before, "закрытый месяц сдвинулся"

        # И правка этой записи тоже идёт заменой, а не переписыванием строки:
        # старая помечается заменённой, новая встаёт рядом, а закрытый месяц
        # по-прежнему не двигается. Переписать строку на месте здесь и нельзя —
        # `facts_guard` не даст тронуть ни одну строку закрытого периода.
        assert client.post(
            URL, payload(item, units, amount="800.00", entry_key=second)
        ).status_code == 302
        rows = facts_of(sql, second)
        assert [row[8] for row in rows] == [1, 2], f"замены не произошло: {rows}"
        assert rows[0][9] is not None and rows[1][9] is None
        assert june_total(sql) == before, "замена сдвинула закрытый месяц"

        # Человеку об этом сказано словами, а не молча: он ввёл июньскую дату и
        # обязан узнать, в каком месяце расход оказался. Месяцы сверяются тем же
        # способом, каким продукт их пишет, — иначе тест проверял бы формат, а
        # не наличие сообщения.
        from web.i18n import month_title

        page = body(client.get(answer["Location"]))
        assert month_title(date(2026, 6, 1)) in page, page
        assert month_title(current_period()) in page, page
    finally:
        client.post("/logout/")


# --- правило 3: повторная запись идёт заменой ---------------------------------


def test_the_same_entry_submitted_again_replaces_the_old_row(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Правка идёт заменой версии, а не переписыванием строки на месте.

    Старая строка помечается заменённой и остаётся историей, новая встаёт
    рядом; действующей остаётся одна, поэтому сумма не удваивается.
    """
    login_as(client, "manager")
    try:
        key = entry_key()
        assert client.post(URL, payload(item, units, entry_key=key)).status_code == 302
        assert client.post(
            URL, payload(item, units, amount="99.00", entry_key=key)
        ).status_code == 302

        rows = facts_of(sql, key)
        assert len(rows) == 2, f"замены не произошло: {rows}"
        old, new = rows
        assert old[8] == 1 and new[8] == 2, "не выставлена версия"
        assert old[9] is not None, "старая строка не помечена заменённой"
        assert old[10] == new[12], "старая строка не ссылается на заменившую"
        assert new[9] is None, "новая строка помечена заменённой"
        assert str(new[1]) == "99.00"

        active = [row for row in rows if row[9] is None]
        assert len(active) == 1, "действующих строк стало больше одной"
    finally:
        client.post("/logout/")


def test_submitting_the_very_same_form_twice_changes_nothing(
    client, sql, item, units, facts_removed,  # noqa: F811
):
    """Двойное нажатие «Сохранить» не превращает один расход в два.

    Ради этого у формы и есть ключ записи: без него обновление страницы после
    внесения означало бы второй расход теми же деньгами.
    """
    login_as(client, "manager")
    try:
        key = entry_key()
        form = payload(item, units, entry_key=key)
        assert client.post(URL, form).status_code == 302
        assert client.post(URL, form).status_code == 302

        rows = facts_of(sql, key)
        assert len(rows) == 1 and rows[0][8] == 1, f"расход записался дважды: {rows}"
    finally:
        client.post("/logout/")
