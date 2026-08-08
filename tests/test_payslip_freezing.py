"""Построчная заморозка ведомости: спорный сотрудник не держит остальных (T027).

Что здесь проверяется и почему именно так.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в этой базе
суперпользователь: политики его не ограничивают вовсе. Проверка запрета,
написанная без переключения роли, зелена всегда — на этом в проекте уже прожил
незамеченным дефект видимости регистров. Поэтому всё, что говорит «база не
даст», идёт через `as_app_user`. Отдельным тестом показано, что числа
замороженной строки держит **триггер**: он отказывает и владельцу тоже, а
политика бы не отказала.

**Заморозка не заморозка, если числа меняются.** Главная проверка задачи —
пересчёт периода: замороженная строка обязана остаться прежней до копейки,
а остальные — пересчитаться.

**Спорная строка никого не держит.** Ни закрытия часов по точке (T022), ни
утверждения периода (T023/T025): на каждый запрет здесь есть парный тест, что
соседнее действие проходит как раньше.

**Три запрета не объясняют один отказ тремя словами.** У утверждённого периода
человек читает про период, а не про строку: порядок сторожей проверяется явно.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from conftest import (
    JUNE,
    T1,
    T2,
    U_BG1,
    U_NS1,
    U_NS2,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
    body,
    login_as,
    period_url,
    wipe_payruns,
)
from test_payrun_lifecycle import new_payrun, payrun_in, rejected, set_status

REASON = "спорные часы: сотрудник не согласен с ночными"


# =============================================================================
# 1. Уровень базы: ролью app_user
# =============================================================================


def make_payslip(conn, payrun_id: str, ext_id: str, unit_id: str | None = None,
                 amount: str = "1000.00", tenant: str = T1) -> str:
    """Строка ведомости с итогами и компонентом. Кладётся владельцем, мимо политик."""
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Спорный') returning id""",
        (tenant, ext_id),
    ).fetchone()[0]
    payslip = conn.execute(
        """insert into payslips (tenant_id, payrun_id, employee_id, unit_id)
           values (%s, %s, %s, %s) returning id""",
        (tenant, payrun_id, employee, unit_id),
    ).fetchone()[0]
    conn.execute(
        """insert into payslip_totals (payslip_id, tenant_id, net, gross)
           values (%s, %s, %s, %s)""",
        (payslip, tenant, amount, amount),
    )
    conn.execute(
        """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
           values (%s, %s, 'hours.regular', 'Часы', %s, 'official')""",
        (tenant, payslip, amount),
    )
    return payslip


def freeze(conn, payslip_id: str, *, reason: str = REASON, actor: str = USER_DIRECTOR,
           tenant: str = T1) -> str:
    return conn.execute(
        """insert into payslip_freezes (tenant_id, payslip_id, reason, frozen_by)
           values (%s, %s, %s, %s) returning id""",
        (tenant, payslip_id, reason, actor),
    ).fetchone()[0]


def release(conn, payslip_id: str, actor: str = USER_DIRECTOR) -> int:
    return conn.execute(
        """update payslip_freezes set released_at = now(), released_by = %s
            where payslip_id = %s and released_at is null""",
        (actor, payslip_id),
    ).rowcount


def amount_of(conn, payslip_id: str) -> Decimal:
    return conn.execute(
        "select amount from pay_components where payslip_id = %s", (payslip_id,)
    ).fetchone()[0]


@pytest.fixture
def disputed(db):
    """Посчитанный период, спорная строка и соседняя — материал почти всех тестов."""
    payrun_id = payrun_in(db, "calculated")
    frozen = make_payslip(db, payrun_id, "freeze-disputed", U_NS1, "1000.00")
    neighbour = make_payslip(db, payrun_id, "freeze-neighbour", U_NS2, "2000.00")
    freeze(db, frozen)
    return {"payrun": payrun_id, "frozen": frozen, "neighbour": neighbour}


def test_a_frozen_row_keeps_its_numbers(disputed, db):
    """Главное правило задачи: сумму замороженной строки не переписать."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        error = rejected(
            conn,
            "update pay_components set amount = 7 where payslip_id = %s",
            (disputed["frozen"],),
        )
        assert "заморожена" in str(error)
        assert amount_of(conn, disputed["frozen"]) == Decimal("1000.00")


def test_a_frozen_row_is_not_deleted(disputed, db):
    """Пересчёт сносит ведомости целиком — на замороженной он обязан споткнуться."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert "заморожена" in str(
            rejected(conn, "delete from payslips where id = %s", (disputed["frozen"],))
        )
        assert "заморожена" in str(
            rejected(
                conn, "delete from pay_components where payslip_id = %s",
                (disputed["frozen"],),
            )
        )
        assert "заморожена" in str(
            rejected(
                conn, "delete from payslip_totals where payslip_id = %s",
                (disputed["frozen"],),
            )
        )
        assert amount_of(conn, disputed["frozen"]) == Decimal("1000.00")


def test_a_frozen_row_does_not_hold_its_neighbours(disputed, db):
    """Спорный сотрудник не держит остальных — на то и построчная блокировка."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute(
            "update pay_components set amount = 2500 where payslip_id = %s",
            (disputed["neighbour"],),
        )
        assert amount_of(conn, disputed["neighbour"]) == Decimal("2500.00")

        conn.execute("delete from pay_components where payslip_id = %s",
                     (disputed["neighbour"],))
        conn.execute("delete from payslip_totals where payslip_id = %s",
                     (disputed["neighbour"],))
        conn.execute("delete from payslips where id = %s", (disputed["neighbour"],))
        assert conn.execute(
            "select count(*) from payslips where id = %s", (disputed["neighbour"],)
        ).fetchone()[0] == 0


def test_the_freeze_holds_against_the_table_owner_too(disputed, db):
    """Держит триггер, а не политика: политику владелец таблиц обходит.

    Тот же довод, что у заморозки утверждённого периода (T023): гарантия,
    зависящая от того, каким пользователем подключились, гарантией не является.
    """
    assert "заморожена" in str(
        rejected(db, "update pay_components set amount = 7 where payslip_id = %s",
                 (disputed["frozen"],))
    )
    assert amount_of(db, disputed["frozen"]) == Decimal("1000.00")


def test_releasing_lets_the_row_change_again(disputed, db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert release(conn, disputed["frozen"]) == 1
        conn.execute("update pay_components set amount = 7 where payslip_id = %s",
                     (disputed["frozen"],))
        assert amount_of(conn, disputed["frozen"]) == Decimal("7.00")


def test_the_release_keeps_the_history(disputed, db):
    """Снятие помечает заморозку, а не удаляет её: «почему морозили» обязано остаться."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        release(conn, disputed["frozen"])
        rows = conn.execute(
            """select reason, released_by is not null from payslip_freezes
                where payslip_id = %s""",
            (disputed["frozen"],),
        ).fetchall()
        assert rows == [(REASON, True)]


def test_a_freeze_is_never_deleted_only_released(disputed, db):
    """Историю, которую можно стереть, историей называть нельзя."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert "пометк" in str(
            rejected(conn, "delete from payslip_freezes where payslip_id = %s",
                     (disputed["frozen"],))
        )


def test_a_freeze_needs_a_reason(db):
    """Заморозка без объяснения через месяц не читается никем — держит схема."""
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-no-reason")

    with as_app_user(db, USER_DIRECTOR) as conn:
        for blank in ("", "   "):
            error = rejected(
                conn,
                """insert into payslip_freezes (tenant_id, payslip_id, reason)
                   values (%s, %s, %s)""",
                (T1, payslip, blank),
            )
            assert "reason" in str(error)
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s", (payslip,)
        ).fetchone()[0] == 0


def test_the_same_row_is_not_frozen_twice(disputed, db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        error = rejected(
            conn,
            """insert into payslip_freezes (tenant_id, payslip_id, reason)
               values (%s, %s, 'ещё раз')""",
            (T1, disputed["frozen"]),
        )
        assert "payslip_freezes_active_uniq" in str(error)


def test_the_same_row_can_be_frozen_again_after_a_release(disputed, db):
    """Спор может вернуться: после снятия строка морозится заново, история копится."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        release(conn, disputed["frozen"])
        freeze(conn, disputed["frozen"], reason="спор вернулся")
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s",
            (disputed["frozen"],),
        ).fetchone()[0] == 2


def test_the_freeze_record_is_not_rewritten(disputed, db):
    """Правится только снятие: причина и автор заморозки — запись в истории."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        assert "снятие" in str(
            rejected(
                conn,
                "update payslip_freezes set reason = 'другое' where payslip_id = %s",
                (disputed["frozen"],),
            )
        )


def test_freezing_needs_its_own_right(db):
    """У управляющего строку видно, а морозить — не его дело: отказывает база."""
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-no-right", U_NS1)

    with as_app_user(db, USER_MANAGER) as conn:
        error = rejected(
            conn,
            """insert into payslip_freezes (tenant_id, payslip_id, reason)
               values (%s, %s, %s)""",
            (T1, payslip, REASON),
        )
        assert "row-level security" in str(error)
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s", (payslip,)
        ).fetchone()[0] == 0


def test_releasing_needs_the_same_right(disputed, db):
    with as_app_user(db, USER_MANAGER) as conn:
        # Правка без права даёт «изменено 0 строк» — тихо, но данные на месте.
        assert release(conn, disputed["frozen"]) == 0
        assert conn.execute(
            """select released_at from payslip_freezes where payslip_id = %s""",
            (disputed["frozen"],),
        ).fetchone()[0] is None


def test_the_accountant_may_freeze_a_row(db):
    """Бухгалтер собирает месяц, поэтому морозить спорную строку вправе он тоже."""
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-accountant")

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        freeze(conn, payslip, actor=USER_ACCOUNTANT)
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s", (payslip,)
        ).fetchone()[0] == 1


def test_a_freeze_of_an_invisible_row_is_invisible(disputed, db):
    """Заморозка видна тому, кому видна её строка ведомости, и никому больше."""
    with as_app_user(db, USER_MANAGER) as conn:
        visible = conn.execute(
            "select payslip_id from payslip_freezes"
        ).fetchall()
        # У управляющего только NS1: спорная строка его точки видна.
        assert [row[0] for row in visible] == [disputed["frozen"]]


def test_freezes_of_another_tenant_are_invisible(disputed, db):
    payrun_other = new_payrun(db, tenant=T2, period=JUNE)
    other = make_payslip(db, payrun_other, "freeze-other-tenant", tenant=T2)
    freeze(db, other, actor=USER_OTHER, tenant=T2)

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s", (other,)
        ).fetchone()[0] == 0


# --- как заморозка строки уживается с двумя другими запретами ----------------


def test_an_approved_period_speaks_about_the_period_not_the_row(disputed, db):
    """Три запрета не объясняют один отказ тремя словами: снаружи внутрь.

    Утверждение периода морозит расчёт целиком, и человеку нужна именно эта
    причина: «строка заморожена» ничего не объясняет там, где заморожено всё.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        set_status(conn, disputed["payrun"], "approved")
        error = str(
            rejected(conn, "update pay_components set amount = 7 where payslip_id = %s",
                     (disputed["frozen"],))
        )
        assert "период утверждён" in error
        assert "заморожена" not in error


def test_a_frozen_row_does_not_hold_the_approval(disputed, db):
    """Спорная строка не держит месяц: период утверждается вместе с ней."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        set_status(conn, disputed["payrun"], "approved")
        assert conn.execute(
            "select status from payruns where id = %s", (disputed["payrun"],)
        ).fetchone()[0] == "approved"


def test_an_approved_period_refuses_new_freezes(db):
    """Внутри утверждённого периода морозить нечего — там заморожено всё."""
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-after-approval")

    with as_app_user(db, USER_DIRECTOR) as conn:
        set_status(conn, payrun_id, "approved")
        error = rejected(
            conn,
            """insert into payslip_freezes (tenant_id, payslip_id, reason)
               values (%s, %s, %s)""",
            (T1, payslip, REASON),
        )
        assert "утверждён" in str(error)


def test_a_freeze_is_released_even_in_an_approved_period(disputed, db):
    """Снятие в утверждённом периоде разрешено: оно ничего не переписывает.

    Иначе замороженная строка застряла бы навсегда в любом закрытом месяце, и
    обслуживание (сид, восстановление из дампа) не смогло бы убрать за собой.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        set_status(conn, disputed["payrun"], "approved")
        assert release(conn, disputed["frozen"]) == 1


def test_a_frozen_row_does_not_block_closing_a_unit(disputed, db):
    """Требование задачи дословно: точка закрывается при спорной строке другой точки.

    Заморозка строки живёт на данных расчёта и табеля не трогает вовсе, поэтому
    на пути записи в часы запрет по-прежнему ровно один — закрытие точки (T022).
    """
    with as_app_user(db, USER_MANAGER) as conn:
        # Спорная строка — на NS2 (см. фикстуру), закрываем свою NS1.
        conn.execute(
            """insert into timesheet_closures (tenant_id, unit_id, period, closed_by)
               values (%s, %s, %s, %s)""",
            (T1, U_NS1, JUNE, USER_MANAGER),
        )
        assert conn.execute(
            """select count(*) from timesheet_closures
                where unit_id = %s and reopened_at is null""",
            (U_NS1,),
        ).fetchone()[0] == 1


def test_a_frozen_row_does_not_block_closing_its_own_unit(db):
    """И своей точки тоже: спор о зарплате не мешает сказать «часы я ввёл».

    Часы замороженного человека правятся как прежде — заморожен результат
    расчёта, а не входные данные (правка задним числом — T026).
    """
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-own-unit", U_NS1)
    freeze(db, payslip)

    with as_app_user(db, USER_MANAGER) as conn:
        conn.execute(
            """insert into timesheet_closures (tenant_id, unit_id, period, closed_by)
               values (%s, %s, %s, %s)""",
            (T1, U_NS1, JUNE, USER_MANAGER),
        )
        assert conn.execute(
            "select count(*) from timesheet_closures where unit_id = %s", (U_NS1,)
        ).fetchone()[0] == 1


def test_the_hours_of_a_frozen_row_are_still_editable(db):
    """Заморожен расчёт, а не табель: иначе на пути записи в часы стало бы два запрета."""
    payrun_id = payrun_in(db, "calculated")
    payslip = make_payslip(db, payrun_id, "freeze-hours", U_BG1)
    freeze(db, payslip)
    employee = db.execute(
        "select employee_id from payslips where id = %s", (payslip,)
    ).fetchone()[0]
    sheet = db.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours, hours)
           values (%s, %s, %s, %s, 176, '{"regular": "8.00"}'::jsonb) returning id""",
        (T1, employee, U_BG1, JUNE),
    ).fetchone()[0]

    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute(
            """update timesheets set hours = '{"regular": "12.00"}'::jsonb where id = %s""",
            (sheet,),
        )
        assert conn.execute(
            "select hours->>'regular' from timesheets where id = %s", (sheet,)
        ).fetchone()[0] == "12.00"


# =============================================================================
# 2. Уровень продукта: живой Django на базе с сидом
# =============================================================================


@pytest.fixture
def clean_payruns(web_env):
    wipe_payruns(web_env)
    yield web_env
    wipe_payruns(web_env)


def sums_by_employee(dsn: str) -> dict[str, Decimal]:
    """Сколько начислено каждому — по внешнему ключу сотрудника."""
    with psycopg.connect(dsn) as conn:
        return {
            row[0]: row[1]
            for row in conn.execute(
                """select e.external_id, sum(c.amount)
                     from pay_components c
                     join payslips p on p.id = c.payslip_id
                     join employees e on e.id = p.employee_id
                    group by e.external_id"""
            ).fetchall()
        }


def any_payslip(dsn: str) -> tuple[str, str]:
    """Строка ведомости, на которой ставится опыт: её id и внешний id сотрудника."""
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """select p.id::text, e.external_id
                 from payslips p join employees e on e.id = p.employee_id
                order by e.external_id limit 1"""
        ).fetchone()


def raise_all_rates(dsn: str) -> None:
    """Поднять ставки всем: после пересчёта числа обязаны разъехаться."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("update employment_terms set base_rate = base_rate * 2")


def restore_rates(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("update employment_terms set base_rate = base_rate / 2")


def calculated_period(client, dsn: str) -> str:
    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    return url


def freeze_url(payslip_id: str) -> str:
    return f"/payslips/{payslip_id}/freeze/"


def release_url(payslip_id: str) -> str:
    return f"/payslips/{payslip_id}/release/"


def test_the_frozen_row_survives_a_recalculation(client, clean_payruns):
    """Заморозка не заморозка, если пересчёт переписывает числа.

    Ставки поднимаются всем сразу: тогда видно и то, что замороженный остался
    прежним до копейки, и то, что остальные действительно пересчитались.
    """
    url = calculated_period(client, clean_payruns)
    payslip_id, external_id = any_payslip(clean_payruns)
    before = sums_by_employee(clean_payruns)

    response = client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)
    assert response.status_code == 200

    raise_all_rates(clean_payruns)
    try:
        client.post(url + "calculate/", follow=True)
        after = sums_by_employee(clean_payruns)
    finally:
        restore_rates(clean_payruns)

    assert after[external_id] == before[external_id]
    changed = [key for key in before if after.get(key) != before[key]]
    assert changed and external_id not in changed
    # Пересчёт не потерял никого: замороженный остался в ведомости.
    assert set(after) == set(before)


def test_the_released_row_is_recalculated_again(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    payslip_id, external_id = any_payslip(clean_payruns)
    before = sums_by_employee(clean_payruns)

    client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)
    client.post(release_url(payslip_id), follow=True)

    raise_all_rates(clean_payruns)
    try:
        client.post(url + "calculate/", follow=True)
        after = sums_by_employee(clean_payruns)
    finally:
        restore_rates(clean_payruns)

    assert after[external_id] != before[external_id]


def test_the_page_marks_the_frozen_row(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    payslip_id, _ = any_payslip(clean_payruns)

    assert "Заморожена" not in body(client.get(url))
    client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)

    page = body(client.get(url))
    assert "Заморожена" in page
    assert REASON in page


def test_freezing_without_a_reason_is_refused_by_the_page(client, clean_payruns):
    calculated_period(client, clean_payruns)
    payslip_id, _ = any_payslip(clean_payruns)

    response = client.post(freeze_url(payslip_id), {"reason": "   "}, follow=True)
    assert response.status_code == 400
    assert "причин" in body(response).lower()

    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from payslip_freezes").fetchone()[0] == 0


def test_a_role_without_the_right_sees_no_button_but_an_explanation(client, clean_payruns):
    """Кнопка не пропадает молча: на её месте тот же текст, которым ответит отказ."""
    calculated_period(client, clean_payruns)
    login_as(client, "manager")

    page = body(client.get(period_url(client)))
    assert "/freeze/" not in page
    assert "Заморозка строки ведомости" in page


def test_freezing_without_the_right_is_refused_past_the_interface(client, clean_payruns):
    calculated_period(client, clean_payruns)
    payslip_id, _ = any_payslip(clean_payruns)
    login_as(client, "manager")

    response = client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)
    assert response.status_code == 403

    with psycopg.connect(clean_payruns) as conn:
        assert conn.execute("select count(*) from payslip_freezes").fetchone()[0] == 0


def test_an_approved_period_refuses_freezing_from_the_page(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    payslip_id, _ = any_payslip(clean_payruns)
    client.post(url + "approve/", follow=True)

    response = client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)
    assert response.status_code == 409
    # Про период, а не про строку: заморожено всё, и человеку нужна эта причина.
    assert "утверждён" in body(response)


def test_the_frozen_row_does_not_hold_the_approval_from_the_page(client, clean_payruns):
    """То же требование глазами человека: месяц утверждается со спорной строкой."""
    url = calculated_period(client, clean_payruns)
    payslip_id, _ = any_payslip(clean_payruns)
    client.post(freeze_url(payslip_id), {"reason": REASON}, follow=True)

    response = client.post(url + "approve/", follow=True)
    assert response.status_code == 200
    assert "Утверждён" in body(response)


def test_the_numbers_of_the_sheet_stay_the_same(client, clean_payruns):
    """Ориентир блока: заморозка ничего не должна сдвинуть в самих числах.

    Итог расчёта директора — 1 951 806,13; он же стоит в журнале блока и в
    смоуке. Проверяется по базе, а не по строке на странице: разделитель
    разрядов в вёрстке неразрывный, и тест ловил бы вёрстку, а не сумму.
    """
    calculated_period(client, clean_payruns)
    with psycopg.connect(clean_payruns) as conn:
        total = conn.execute("select sum(amount) from pay_components").fetchone()[0]
    assert total == Decimal("1951806.13")
