"""Касса как справочник, и регистр расхода из неё (T145, D039).

Проверяется ровно то, ради чего задача заведена, — ответ владельца на Q013:
«Из кассы берем только официально. Но есть чёрная касса, где по дефолту идёт в
чёрную». То есть регистр учёта расхода следует из **источника денег**, а не
выбирается к каждой трате.

Четыре правила и как каждое проверено:

1. **Касса заводится с экрана** — обычным справочником, тем же правом
   `directory.manage`, что и остальные шесть.
2. **Регистр расхода приезжает из кассы**, а ручной выбор остаётся сильнее её.
3. **Роль видит только кассы своих точек**, и отвергает чужую **база**: в POST
   кладётся номер чужой кассы, и запись обязана не пройти. Сломайте политику
   `till_visibility` на `facts` — и проверка покраснеет.
4. **Отказ по чужой кассе неотличим от «такой нет»** (D023): по ответу нельзя
   понять, что касса существует у другой точки.

Расходы без кассы при этом остаются законными: так внесено всё, что было до
этой задачи, и регистр у них уже проставлен.
"""
from __future__ import annotations

import re
import uuid

import pytest

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    URL,
    entry_key,
    facts_of,
    facts_removed,
    item,
    payload,
    tenant,
    units,
)
from test_directory import sql  # noqa: F401

TILLS_URL = "/directory/tills/"
NEW_TILL = "/directory/tills/new/"


@pytest.fixture
def tills(sql, tenant, units):  # noqa: F811
    """Четыре кассы: две официальные и внутренняя на NS1, официальная на BG1.

    Кладутся владельцем схемы — это подготовка материала, а не проверка права
    их заводить: право проверяется отдельным тестом через экран.
    """
    made = {}
    for code, unit_code, ledger in (
        ("NS1-main", "NS1", "official"),
        # Вторая официальная касса той же точки нужна ровно одной проверке:
        # смене **только** кассы. Через кассу другой точки её не проверить —
        # там вместе с кассой меняется и точка, и `facts_same` увидела бы
        # изменение даже без колонки `till_id`.
        ("NS1-spare", "NS1", "official"),
        ("NS1-second", "NS1", "internal"),
        ("BG1-main", "BG1", "official"),
    ):
        made[code] = str(sql.execute(
            """insert into tills (tenant_id, unit_id, code, title, ledger)
               values (%s, %s, %s, %s, %s::ledger) returning id""",
            (tenant, units[unit_code], code, code, ledger),
        ).fetchone()[0])
    yield made
    sql.execute("delete from facts where till_id = any(%s::uuid[])", (list(made.values()),))
    sql.execute("delete from tills where id = any(%s::uuid[])", (list(made.values()),))


def _message(html: str) -> str:
    found = re.search(r'class="alert"[^>]*>(.*?)</div>', html, re.S)
    return re.sub(r"\s+", " ", found.group(1)).strip() if found else ""


def till_of(sql, key: str):  # noqa: F811
    return sql.execute(
        "select till_id::text, ledger::text, unit_id::text from facts "
        "where dedup_key = %s and superseded_at is null",
        (f"manual:cash:{key}",),
    ).fetchone()


# --- справочник ----------------------------------------------------------------


def test_the_admin_creates_a_till_from_the_screen(client, sql, units):  # noqa: F811
    """Касса заводится с экрана — иначе справочник наполнять нечем."""
    login_as(client, "admin")
    try:
        answer = client.post(NEW_TILL, {
            "code": "smoke-till", "title": "Касса смоука",
            "unit": units["NS1"], "ledger": "internal",
        })
        assert answer.status_code == 302, body(answer)

        stored = sql.execute(
            "select unit_id::text, ledger::text from tills where code = 'smoke-till'"
        ).fetchone()
        assert stored == (units["NS1"], "internal"), stored
        assert "smoke-till" in body(client.get(TILLS_URL))
    finally:
        sql.execute("delete from tills where code = 'smoke-till'")
        client.post("/logout/")


def test_a_role_without_the_right_does_not_keep_the_directory(client, tills):  # noqa: F811
    """Ведёт кассы тот же, кто ведёт остальные справочники, и только он."""
    login_as(client, "manager")
    try:
        assert client.get(TILLS_URL).status_code == 403
    finally:
        client.post("/logout/")


# --- правило: регистр приезжает из кассы ---------------------------------------


def test_the_expense_takes_its_ledger_from_the_till(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Главное решение задачи: регистр следует из кассы, а не из поля формы.

    Директор выбирает внутреннюю кассу и **не трогает** регистр — расход обязан
    лечь во внутренний регистр. Так владелец и описывает происходящее: «есть
    чёрная касса, где по дефолту идёт в чёрную».
    """
    login_as(client, "director")
    try:
        key = entry_key()
        answer = client.post(URL, payload(
            item, units, till=tills["NS1-second"], ledger="", entry_key=key,
        ))
        assert answer.status_code == 302, body(answer)

        till_id, ledger, unit_id = till_of(sql, key)
        assert till_id == tills["NS1-second"], "касса не записана в факт"
        assert ledger == "internal", f"регистр взят не из кассы: {ledger}"
        assert unit_id == units["NS1"], "точка взята не из кассы"
    finally:
        client.post("/logout/")


def test_the_ledger_chosen_by_hand_is_stronger_than_the_till(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Ручная правка регистра остаётся возможной — она лишь перестала быть главной."""
    login_as(client, "director")
    try:
        key = entry_key()
        assert client.post(URL, payload(
            item, units, till=tills["NS1-second"], ledger="official", entry_key=key,
        )).status_code == 302
        assert till_of(sql, key)[1] == "official"
    finally:
        client.post("/logout/")


def test_an_expense_without_a_till_still_works(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Расход без кассы — законное состояние: так внесено всё, что было раньше."""
    login_as(client, "manager")
    try:
        key = entry_key()
        assert client.post(URL, payload(item, units, entry_key=key)).status_code == 302
        till_id, ledger, _unit = till_of(sql, key)
        assert till_id is None and ledger == "official"
    finally:
        client.post("/logout/")


def test_changing_only_the_till_counts_as_a_change(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Смена одной только кассы обязана завести новую версию факта.

    Иначе `facts_same` считала бы событие тем же самым, и правка молча не
    применилась бы — ровно так однажды терялась статья расхода.
    """
    login_as(client, "director")
    try:
        key = entry_key()
        assert client.post(URL, payload(
            item, units, till=tills["NS1-main"], ledger="official", entry_key=key,
        )).status_code == 302
        assert client.post(URL, payload(
            item, units, till=tills["NS1-spare"], ledger="official", entry_key=key,
        )).status_code == 302

        rows = facts_of(sql, key)
        assert [row[8] for row in rows] == [1, 2], f"замены не произошло: {rows}"
        assert till_of(sql, key)[0] == tills["NS1-spare"]
    finally:
        client.post("/logout/")


# --- правило: видно только кассы своих точек ------------------------------------


def test_the_manager_sees_only_the_tills_of_his_units(
    client, item, units, tills, facts_removed,  # noqa: F811
):
    """Список касс в форме — только свои точки и только видимые регистры.

    Внутренней кассы своей же точки управляющий не видит: внутреннего регистра
    ему не видно вовсе (D031), и предложить кассу, из которой запись всё равно
    отвергнет `ledger_visibility` на `facts`, значило бы обещать отказ.
    """
    login_as(client, "manager")
    try:
        page = body(client.get(URL))
        assert "NS1-main" in page, "своя касса не предложена"
        assert "BG1-main" not in page, "в форме видна касса чужой точки"
        assert "NS1-second" not in page, "в форме видна касса невидимого регистра"
    finally:
        client.post("/logout/")


def test_choosing_another_units_till_is_refused_by_the_database(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Главная проверка доступа: подмена кассы в запросе отвергается базой.

    Представление кассу не фильтрует — оно передаёт номер как есть, и отвергает
    его политика `till_visibility` на `facts`. Сломайте её (`using (true)`) — и
    чужая касса запишется, а тест покраснеет.
    """
    login_as(client, "manager")
    try:
        key = entry_key()
        answer = client.post(URL, payload(
            item, units, till=tills["BG1-main"], entry_key=key,
        ))
        assert answer.status_code == 400, body(answer)
        assert facts_of(sql, key) == [], "расход лёг с чужой кассой"
    finally:
        client.post("/logout/")


def test_the_till_of_an_invisible_ledger_is_refused_too(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Касса своей точки, но невидимого регистра, тоже не проходит.

    Регистр входит в правило видимости кассы наравне с точкой, иначе
    управляющий записал бы расход в регистр, которого потом не найдёт.
    """
    login_as(client, "manager")
    try:
        key = entry_key()
        answer = client.post(URL, payload(
            item, units, till=tills["NS1-second"], entry_key=key,
        ))
        assert answer.status_code == 400, body(answer)
        assert facts_of(sql, key) == [], "расход лёг с кассой невидимого регистра"
    finally:
        client.post("/logout/")


def test_the_refusal_does_not_reveal_that_the_till_exists(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Чужая касса и несуществующая отвечают одинаково (D023)."""
    login_as(client, "manager")
    try:
        alien = client.post(URL, payload(
            item, units, till=tills["BG1-main"], entry_key=entry_key(),
        ))
        nobody = client.post(URL, payload(
            item, units, till=str(uuid.uuid4()), entry_key=entry_key(),
        ))
        assert alien.status_code == nobody.status_code
        assert _message(body(alien)) == _message(body(nobody)), (
            "по тексту отказа видно, что чужая касса существует"
        )
    finally:
        client.post("/logout/")


def test_the_directory_screen_hides_another_units_till(client, tills):  # noqa: F811
    """Сам справочник тоже режется политиками, а не выборкой экрана.

    Проверяется администратором сети: у него точки не ограничены, поэтому здесь
    видно все три кассы — а значит, отсутствие чужой у управляющего в форме
    выше и есть работа политик, а не пустой справочник.
    """
    login_as(client, "admin")
    try:
        page = body(client.get(TILLS_URL))
        assert "NS1-main" in page and "BG1-main" in page and "NS1-second" in page
    finally:
        client.post("/logout/")


# --- правило: касса и точка не спорят -------------------------------------------


def test_a_till_and_a_different_unit_are_refused_in_words(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """Касса стоит на точке, и второго ответа «где это было» быть не может.

    Молча взять одно из двух значило бы записать расход не туда, куда человек
    думал, — поэтому продукт отказывает словами и называет точку кассы.
    """
    login_as(client, "director")
    try:
        key = entry_key()
        answer = client.post(URL, payload(
            item, units, till=tills["NS1-main"], unit=units["BG1"], entry_key=key,
        ))
        assert answer.status_code == 400, body(answer)
        assert "NS1" in _message(body(answer)), _message(body(answer))
        assert facts_of(sql, key) == []
    finally:
        client.post("/logout/")


def test_a_network_expense_cannot_be_paid_from_a_till(
    client, sql, item, units, tills, facts_removed,  # noqa: F811
):
    """«Вся сеть» и касса несовместимы: у кассы точка есть, у расхода сети — нет."""
    from web.cash import NETWORK_UNIT

    login_as(client, "director")
    try:
        key = entry_key()
        answer = client.post(URL, payload(
            item, units, till=tills["NS1-main"], unit=NETWORK_UNIT, entry_key=key,
        ))
        assert answer.status_code == 400, body(answer)
        assert facts_of(sql, key) == []
    finally:
        client.post("/logout/")


# --- политики проверяются ролью приложения, а не владельцем схемы ----------------


def test_the_policies_hold_under_the_app_role(web_env, sql, tenant, tills):  # noqa: F811
    """Срез касс делает RLS, и проверяется он ролью `app_user`.

    Владелец таблиц политики обходит, поэтому проверка под ним зеленела бы и
    при снятых политиках — на этом проекте дефект видимости регистров уже
    прожил незамеченным ровно так. Соединение здесь своё и **без**
    `autocommit`: `set local role` действует внутри транзакции, а без неё
    молча ничего не переключает.
    """
    import psycopg

    from conftest import as_app_user

    manager = str(sql.execute(
        "select m.user_id from memberships m join roles r on r.id = m.role_id "
        "where r.code = 'manager' and m.tenant_id = %s", (tenant,)
    ).fetchone()[0])

    with psycopg.connect(web_env) as conn:
        with as_app_user(conn, manager):
            seen = {row[0] for row in conn.execute("select code from tills").fetchall()}
            visible = conn.execute(
                "select app_till_is_visible(%s, %s), app_till_is_visible(%s, %s)",
                (tenant, tills["NS1-main"], tenant, tills["BG1-main"]),
            ).fetchone()
        conn.rollback()

    assert seen == {"NS1-main", "NS1-spare"}, seen
    assert visible == (True, False), visible
