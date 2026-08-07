"""Ведомость не должна выдавать скрытый регистр вычитанием (T050, issue #42).

Суть дефекта. Ограничивающая политика висела на `pay_components`, а у
`payslips` признака регистра не было вовсе, хотя `net`, `gross`, `tax`,
`contributions`, `total_cost`, `to_bank`, `to_cash` посчитаны **по всем
регистрам сразу**. Достаточно одного экрана или выгрузки, показавшей `net`, — и
скрытая часть восстанавливается вычитанием: `net − сумма видимых компонентов`.
Требование D023 («ни строк, ни следа в итогах») было соблюдено на компонентах и
нарушено на ведомости.

Как починено: суммы уехали в отдельную таблицу `payslip_totals`, и ограничивающая
политика стоит на ней. Прятать саму строку `payslips` нельзя — ведомость
собирается присоединением к ней, и скрытая строка утащила бы за собой **видимые**
официальные компоненты смешанного сотрудника.

Требование к решению: роль, которой виден только официальный регистр, не должна
получить из итогов число, в котором есть хоть рубль скрытого от неё. Оно
проверяется здесь в точной форме — **каждая видимая строка итогов посчитана
только из видимых компонентов**: `net` видимой строки в точности равен сумме её
компонентов, которые роль видит.

Почему не «ни одного числа больше суммы видимых компонентов» буквально: `gross`
и взносы больше нето по построению (обратный пересчёт нето → бруто даёт
`net / 0.701`), и превышают сумму компонентов даже у строки, где скрытых
регистров нет вовсе. Сравнивать их с суммой компонентов бессмысленно; значение
имеет то, что они посчитаны от нето, в котором нет ни одного скрытого рубля.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import (
    JUNE,
    T1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
)


def make_payslip(conn, ext_id: str, components: list[tuple[str, str]]) -> str:
    """Ведомость с компонентами `[(регистр, сумма)]` и суммарными полями по всем.

    Суммарные поля заполняются намеренно «как считает движок» — по всем
    регистрам разом: именно это и утекало.
    """
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (T1, ext_id),
    ).fetchone()[0]
    payrun = conn.execute(
        """insert into payruns (tenant_id, period) values (%s, %s)
           on conflict (tenant_id, period) do update set period = excluded.period
           returning id""",
        (T1, JUNE),
    ).fetchone()[0]
    total = sum(Decimal(amount) for _, amount in components)
    payslip = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id)
           values (%s, %s, %s) returning id""",
        (T1, payrun, employee),
    ).fetchone()[0]
    conn.execute(
        """insert into payslip_totals (tenant_id, payslip_id, net, gross, total_cost)
           values (%s, %s, %s, %s, %s)""",
        (T1, payslip, total, total * 2, total * 3),
    )
    for index, (ledger, amount) in enumerate(components):
        conn.execute(
            """insert into pay_components
                   (tenant_id, payslip_id, code, title, amount, ledger)
               values (%s, %s, %s, 'Часы', %s, %s)""",
            (T1, payslip, f"hours.{index}", amount, ledger),
        )
    return payslip


def visible_totals(conn) -> list[tuple]:
    return conn.execute("select payslip_id, net from payslip_totals").fetchall()


# --- главное требование ------------------------------------------------------


def test_totals_of_a_mixed_payslip_are_invisible_to_the_accountant(db):
    """Итоги строки из видимого и скрытого не показываются вовсе.

    Показать их «частично» нельзя: `net` — одно число на все регистры, и любое
    его появление на экране выдаёт скрытую часть вычитанием.
    """
    make_payslip(db, "mixed", [("official", "100.00"), ("internal", "900.00")])

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert visible_totals(conn) == []

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert len(visible_totals(conn)) == 1, "директор обязан видеть итоги целиком"

    # А сама строка ведомости видна обоим: на ней держится ведомость по
    # компонентам, и её пропажа отняла бы у бухгалтера видимые ей суммы.
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert conn.execute("select count(*) from payslips").fetchone()[0] == 1


def test_totals_of_a_fully_official_payslip_stay_visible(db):
    """Ограничение не должно превращаться в «бухгалтер не видит ничего»."""
    make_payslip(db, "clean", [("official", "500.00")])

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        rows = visible_totals(conn)
    assert [row[1] for row in rows] == [Decimal("500.00")]


def each_visible_payslip_is_built_from_visible_components(conn) -> int:
    """Нето каждой видимой строки = сумма её видимых компонентов. Вернёт их число.

    Это и есть «ни рубля скрытого»: нето по определению равно сумме всех
    компонентов строки, поэтому равенство с суммой **видимых** означает, что
    невидимых у неё нет.
    """
    rows = conn.execute(
        """select t.payslip_id, t.net, coalesce(sum(c.amount), 0)
             from payslip_totals t
             left join pay_components c on c.payslip_id = t.payslip_id
            group by t.payslip_id, t.net"""
    ).fetchall()
    for payslip_id, net, visible in rows:
        assert net == visible, f"строка {payslip_id}: нето {net}, видимых компонентов {visible}"
    return len(rows)


def test_visible_payslips_contain_nothing_hidden(db):
    """Главная проверка задачи, в точной форме."""
    make_payslip(db, "mixed-1", [("official", "100.00"), ("supplementary", "400.00")])
    make_payslip(db, "mixed-2", [("official", "50.00"), ("internal", "700.00")])
    make_payslip(db, "clean-1", [("official", "300.00")])

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert each_visible_payslip_is_built_from_visible_components(conn) == 1
        visible_components = conn.execute(
            "select coalesce(sum(amount), 0) from pay_components"
        ).fetchone()[0]
        net = conn.execute("select coalesce(sum(net), 0) from payslip_totals").fetchone()[0]

    # Бухгалтеру видны 450 из компонентов (100 + 50 + 300), а ведомостей — одна,
    # на 300: две смешанные строки скрыты целиком.
    assert visible_components == Decimal("450.00")
    assert net == Decimal("300.00")

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert each_visible_payslip_is_built_from_visible_components(conn) == 3


def test_manager_sees_two_ledgers_but_not_the_third(db):
    """Проверка не сводится к «бухгалтеру не видно»: границу двигает роль."""
    make_payslip(db, "two", [("official", "100.00"), ("supplementary", "200.00")])
    make_payslip(db, "three", [("supplementary", "200.00"), ("internal", "300.00")])

    with as_app_user(db, USER_MANAGER) as conn:  # официальный + дополнительный
        rows = visible_totals(conn)
    assert [row[1] for row in rows] == [Decimal("300.00")]


# --- как это устроено --------------------------------------------------------


def test_ledgers_column_follows_the_components(db):
    """Набор регистров строки поддерживает база, а не тот, кто пишет.

    Пишут в `payslips` расчёт, импорт и правки; полагаться на то, что каждый из
    них не забудет проставить поле, — это ровно та дисциплина в коде, от которой
    задача и уходит.
    """
    payslip = make_payslip(db, "tracked", [("official", "10.00"), ("internal", "20.00")])
    ledgers = db.execute("select ledgers from payslips where id = %s", (payslip,)).fetchone()[0]
    assert sorted(ledgers) == ["internal", "official"]


def test_totals_without_components_are_not_hidden(db):
    """Пустая строка остаётся видимой — иначе её нельзя было бы удалить.

    Расчёт пересобирает ведомость: сносит компоненты, потом строки. Если бы
    строка без компонентов становилась невидимой, `delete` перестал бы её
    находить и повторный расчёт молча плодил бы дубли. Раскрыть она ничего не
    может: компонентов у неё нет.
    """
    payslip = make_payslip(db, "empty", [("official", "10.00")])
    db.execute("delete from pay_components where payslip_id = %s", (payslip,))

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert conn.execute(
            "select count(*) from payslip_totals where payslip_id = %s", (payslip,)
        ).fetchone()[0] == 1


def test_hiding_is_not_blanket(db):
    """Страховка от фиктивной зелени: без скрытых компонентов ничего не прячется."""
    make_payslip(db, "visible-1", [("official", "10.00")])
    make_payslip(db, "visible-2", [("official", "20.00")])

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert len(visible_totals(conn)) == 2


def test_the_protection_is_the_policy_and_not_luck(db):
    """Снимаем политику — проверка обязана покраснеть.

    Иначе нельзя отличить «закрыто» от «в тестовых данных просто нечему было
    утечь». Политика снимается внутри транзакции теста и возвращается с её
    откатом.
    """
    make_payslip(db, "leak", [("official", "100.00"), ("internal", "900.00")])
    db.execute("drop policy ledger_visibility on payslip_totals")

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        assert len(visible_totals(conn)) == 1, "без политики строка обязана быть видна"
        with pytest.raises(AssertionError):
            each_visible_payslip_is_built_from_visible_components(conn)


# =============================================================================
# Факт наличия скрытого регистра (T065)
# =============================================================================
# Суммы закрыты выше, а `payslips.ledgers` до T065 читался всеми в тенанте.
# Ролью `app_user` под бухгалтером запрос
#     select e.last_name, p.ledgers from payslips p join employees e ...
# отдавал пары «Курир → {internal}», «ANDRIC → {official, supplementary}»:
# поимённо видно, у кого есть выплаты в закрытых от неё регистрах. Чисел нет,
# но D023 требует «ни строк, ни следа» — а это след.
#
# Почему починено привилегией на колонку, а не политикой и не ещё одной
# таблицей: набор регистров нужен **самой базе** (на нём стоит видимость итогов)
# и не нужен приложению ни в одной роли. Прятать строку `payslips` нельзя — на
# этом уже обжигались (см. шапку модуля). Показать значение колонки одной роли и
# скрыть от другой RLS не умеет: она режет строки, а не столбцы. Зато колонку
# можно закрыть от роли приложения целиком — политика и триггер читают её
# правами владельца и работают по-прежнему.

PAYSLIP_COLUMNS_FOR_THE_APP = {
    "id", "tenant_id", "payrun_id", "employee_id", "unit_id", "notes",
}


def granted_columns(conn, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            """select column_name from information_schema.column_privileges
                where grantee = 'app_user' and table_name = %s and privilege_type = 'SELECT'""",
            (table,),
        ).fetchall()
    }


def test_the_set_of_ledgers_is_unreadable_by_the_application(db):
    """Главная проверка задачи: значения колонки не получает ни одна роль."""
    import psycopg

    make_payslip(db, "mixed", [("official", "100.00"), ("internal", "900.00")])

    for user in (USER_ACCOUNTANT, USER_MANAGER, USER_DIRECTOR):
        with as_app_user(db, user) as conn:
            conn.execute("savepoint attempt")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select ledgers from payslips").fetchall()
            conn.execute("rollback to savepoint attempt")

            # И через звёздочку тоже: иначе первый же `select *` в отчёте
            # вернул бы то, что закрыто поимённо.
            conn.execute("savepoint attempt")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select * from payslips").fetchall()
            conn.execute("rollback to savepoint attempt")


def test_the_sheet_still_shows_official_components_of_a_mixed_person(db):
    """Цена ошибки прошлого захода: строку `payslips` прятать нельзя.

    Ведомость собирается присоединением к `payslips`, и спрятанная строка
    утаскивает за собой видимые официальные компоненты смешанного сотрудника.
    Здесь проверяется, что починка факта регистров этого не повторила.
    """
    make_payslip(db, "mixed", [("official", "100.00"), ("internal", "900.00")])

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        rows = conn.execute(
            """select c.amount from pay_components c
                 join payslips p on p.id = c.payslip_id"""
        ).fetchall()
    assert [row[0] for row in rows] == [Decimal("100.00")]


def test_the_columns_the_application_may_read_are_listed_on_purpose(db):
    """Сторож на будущее: новая колонка `payslips` должна выдаваться явно.

    Табличная привилегия снята, поэтому колонки выданы поимённо. Если кто-то
    добавит колонку и не выдаст её, приложение получит `permission denied for
    table payslips` — этот тест обязан упасть раньше и объяснить, что делать.
    """
    all_columns = {
        row[0]
        for row in db.execute(
            "select column_name from information_schema.columns where table_name = 'payslips'"
        ).fetchall()
    }
    assert granted_columns(db, "payslips") == PAYSLIP_COLUMNS_FOR_THE_APP, (
        "список колонок, доступных роли приложения, разъехался с миграцией "
        "0021_payslip_ledgers_hidden — выдайте новую колонку явно"
    )
    assert all_columns - PAYSLIP_COLUMNS_FOR_THE_APP == {"ledgers"}, (
        f"в payslips появилась колонка без привилегии: {sorted(all_columns)}"
    )


def test_the_trigger_still_fills_the_set_under_the_application_role(db):
    """Триггер обязан работать из-под роли, которая колонку не читает.

    Он читает и пишет `ledgers`, поэтому исполняется правами владельца
    (`security definer`). Отменить это нельзя: расчёт упал бы отказом в
    привилегии, а набор регистров, собранный не до конца, открыл бы итоги
    строки тому, кому они закрыты.
    """
    payslip = make_payslip(db, "by-app", [("official", "10.00")])
    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute(
            """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
               values (%s, %s, 'hours.extra', 'Часы', 20, 'internal')""",
            (T1, payslip),
        )
    ledgers = db.execute("select ledgers from payslips where id = %s", (payslip,)).fetchone()[0]
    assert sorted(ledgers) == ["internal", "official"]


def test_the_protection_is_the_privilege_and_not_luck(db):
    """Возвращаем табличную привилегию — проверка обязана покраснеть."""
    make_payslip(db, "leak", [("official", "100.00"), ("internal", "900.00")])
    db.execute("savepoint before_damage")
    db.execute("grant select on payslips to app_user")
    try:
        with as_app_user(db, USER_ACCOUNTANT) as conn:
            leaked = conn.execute("select ledgers from payslips").fetchall()
        assert sorted(leaked[0][0]) == ["internal", "official"], (
            "без отзыва привилегии бухгалтер обязан видеть чужие регистры — "
            "иначе тест ничего не проверяет"
        )
    finally:
        db.execute("rollback to savepoint before_damage")


# --- на данных сида ----------------------------------------------------------


def test_accountant_cannot_reconstruct_hidden_ledgers_on_seeded_data(client, web_env):
    """То же требование на настоящем расчёте месяца, а не на выдуманных строках."""
    import psycopg

    from conftest import login_as, period_url, wipe_payruns

    wipe_payruns(web_env)
    login_as(client, "director")
    client.post(period_url(client) + "calculate/", follow=True)

    # Пользователи сида — не те, что в фикстуре тестов схемы: у сида свои
    # детерминированные id, и брать их надо у него, а не переписывать.
    from core.management.commands.seed_dev import det_id

    with psycopg.connect(web_env) as conn:
        from core.db_types import register_enum_types

        register_enum_types(conn)
        with as_app_user(conn, str(det_id("user", "accountant"))) as scoped:
            components = scoped.execute(
                "select coalesce(sum(amount), 0) from pay_components"
            ).fetchone()[0]
            net = scoped.execute(
                "select coalesce(sum(net), 0) from payslip_totals"
            ).fetchone()[0]
            slips = each_visible_payslip_is_built_from_visible_components(scoped)

    assert components > 0, "расчёт не прошёл — проверять нечего"
    assert slips > 0, "бухгалтеру не видно ни одной строки — проверка стала бы пустой"
    assert net <= components, f"нето {net} выдаёт скрытое: видимых компонентов {components}"

