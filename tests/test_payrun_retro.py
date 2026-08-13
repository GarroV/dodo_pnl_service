"""Правки задним числом: разница едет вперёд, закрытый месяц прежний (T026).

Что здесь проверяется и почему именно так.

**Главная проверка — снимок.** Задача считается сделанной, когда видно, что
закрытый период остался прежним **байт в байт**. Поэтому центральный тест не
смотрит на отдельные суммы, а снимает построчный слепок всего, что закрытый
месяц хранит (расчёт, строки ведомости, итоги, компоненты), проводит правку и
перенос, и сверяет слепок целиком. Проверка «итог не изменился» пропустила бы
перестановку сумм между людьми.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в этой базе
суперпользователь: политики его не ограничивают вовсе. Всё, что говорит «база не
даст», идёт через `as_app_user`. Отдельно показано, где гарантию держит
**триггер**: он отказывает и владельцу тоже, а политика бы не отказала.

**Двойной счёт проверяется с обеих сторон.** Перенесли дважды — не задвоилось;
источник пересчитали — перенос отменён; разница утверждена у получателя —
источник не открывается заново.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from conftest import (
    JULY,
    JUNE,
    T1,
    U_BG1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
    body,
    login_as,
    wipe_payruns,
)
from test_payrun_lifecycle import payrun_in, rejected, set_status, status_of

AUGUST = "2026-08-01"


# =============================================================================
# 1. Уровень базы: ролью app_user
# =============================================================================


def make_slip(conn, payrun_id: str, ext_id: str, *, unit_id: str | None = U_BG1,
              amount: str = "1000.00", ledger: str = "official",
              tenant: str = T1, retro_source: str | None = None) -> tuple[str, str]:
    """Строка ведомости с компонентом. Кладётся владельцем, мимо политик."""
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Ретров') returning id""",
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
        """insert into pay_components
               (tenant_id, payslip_id, code, title, amount, ledger, retro_source_period)
           values (%s, %s, 'hours.regular', 'Часы', %s, %s, %s)""",
        (tenant, payslip, amount, ledger, retro_source),
    )
    return employee, payslip


def approved_with(conn, ext_ids, *, period: str = JUNE, **kw) -> list[tuple[str, str]]:
    """Утверждённый месяц с готовой ведомостью.

    Строки кладутся **до** утверждения: в утверждённый расчёт база писать не
    даёт (T023) — ровно тот запрет, ради обхода которого и придумана дельта.
    """
    payrun = payrun_in(conn, "calculated", period=period)
    made = [make_slip(conn, payrun, ext, **kw) for ext in ext_ids]
    set_status(conn, payrun, "approved")
    return [payrun, *made]


def employ(conn, employee: str, unit_id: str, *, tenant: str = T1) -> None:
    """Условия найма: по ним база решает, свой ли это человек для управляющего."""
    group = conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme, ledger)
           values (%s, %s, 'Тест', 'hourly', 'official')
           on conflict (tenant_id, code) do update set title = excluded.title
           returning id""",
        (tenant, "retro-test"),
    ).fetchone()[0]
    conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, unit_id, group_id, valid_from, base_rate, coefficient)
           values (%s, %s, %s, %s, '2023-01-01', 100, 1)""",
        (tenant, employee, unit_id, group),
    )


def adjust(conn, *, employee: str, source: str = JUNE, target: str = JULY,
           amount: str = "500.00", ledger: str = "official", tenant: str = T1,
           unit_id: str | None = U_BG1, actor: str = USER_DIRECTOR) -> str:
    return conn.execute(
        """insert into retro_adjustments
               (tenant_id, source_period, target_period, employee_id, unit_id,
                code, title, amount, ledger, created_by)
           values (%s, %s, %s, %s, %s, 'hours.regular', 'Часы', %s, %s, %s)
           returning id""",
        (tenant, source, target, employee, unit_id, amount, ledger, actor),
    ).fetchone()[0]


def test_the_adjustment_lands_in_the_ledger_it_is_given(db):
    """Регистр переноса — свой, а не выведенный из человека или из периода."""
    june, (employee, _) = approved_with(db, ["retro-ledger"], ledger="supplementary")
    row = adjust(db, employee=employee, ledger="supplementary")

    assert db.execute(
        "select ledger from retro_adjustments where id = %s", (row,)
    ).fetchone()[0] == "supplementary"


def test_the_accountant_does_not_see_a_supplementary_adjustment(db):
    """Разница живёт в том же регистре, и чужой регистр не виден — как у сумм.

    Иначе бухгалтер увидел бы в переносе то, что скрыто от него в самой
    ведомости: дополнительный регистр вычитался бы из «строки есть, суммы нет».
    """
    june = payrun_in(db, "calculated")
    open_one, _ = make_slip(db, june, "retro-vis-official")
    hidden, _ = make_slip(db, june, "retro-vis-supp", ledger="supplementary")
    set_status(db, june, "approved")
    adjust(db, employee=open_one, ledger="official")
    adjust(db, employee=hidden, ledger="supplementary")

    with as_app_user(db, USER_ACCOUNTANT):
        seen = db.execute(
            "select ledger from retro_adjustments order by ledger"
        ).fetchall()
    assert seen == [("official",)], "бухгалтеру виден дополнительный регистр"

    with as_app_user(db, USER_DIRECTOR):
        assert len(db.execute("select 1 from retro_adjustments").fetchall()) == 2


def test_the_manager_does_not_see_an_adjustment_of_another_unit(db):
    """Управляющий видит переносы только по своим людям — правилом самой базы."""
    june = payrun_in(db, "calculated")
    mine, _ = make_slip(db, june, "retro-mine", unit_id=U_NS1)
    theirs, _ = make_slip(db, june, "retro-theirs", unit_id=U_BG1)
    set_status(db, june, "approved")
    employ(db, mine, U_NS1)
    employ(db, theirs, U_BG1)
    adjust(db, employee=mine, unit_id=U_NS1)
    adjust(db, employee=theirs, unit_id=U_BG1)

    with as_app_user(db, USER_MANAGER):
        seen = db.execute("select employee_id from retro_adjustments").fetchall()
    assert seen == [(mine,)], "управляющий видит перенос по чужой точке"


def test_another_tenant_does_not_see_the_adjustment(db):
    june, (employee, _) = approved_with(db, ["retro-isolation"])
    adjust(db, employee=employee)

    with as_app_user(db, USER_OTHER):
        assert db.execute("select 1 from retro_adjustments").fetchall() == []


def test_posting_requires_the_right(db):
    """Без права `retro.post` перенос отвергает база, а не только форма."""
    june, (employee, _) = approved_with(db, ["retro-right"])
    employ(db, employee, U_NS1)

    with as_app_user(db, USER_MANAGER):
        error = rejected(
            db,
            """insert into retro_adjustments
                   (tenant_id, source_period, target_period, employee_id,
                    code, title, amount, ledger)
               values (%s, %s, %s, %s, 'hours.regular', 'Часы', 100, 'official')""",
            (T1, JUNE, JULY, employee),
        )
    assert "row-level security" in str(error)


def test_the_right_actually_lets_the_transfer_through(db):
    """Парная проверка к запрету: с правом перенос проходит.

    Без неё тест на отказ был бы зелёным и у политики, запрещающей всем: «нельзя
    никому» и «нельзя без права» с одной стороны неотличимы.
    """
    june, (employee, _) = approved_with(db, ["retro-allowed"])
    employ(db, employee, U_NS1)

    with as_app_user(db, USER_DIRECTOR):
        row = db.execute(
            """insert into retro_adjustments
                   (tenant_id, source_period, target_period, employee_id,
                    code, title, amount, ledger)
               values (%s, %s, %s, %s, 'hours.regular', 'Часы', 100, 'official')
               returning id""",
            (T1, JUNE, JULY, employee),
        ).fetchone()[0]
    assert row


def test_the_cancellation_cannot_be_written_by_hand(db):
    """Отмена переноса ставится только триггером — руками её не проставить.

    Иначе «отмена без пересчёта источника» стала бы отдельным действием, и
    двойной счёт вернулся бы через дверь, которую специально закрывали.
    """
    june, (employee, _) = approved_with(db, ["retro-by-hand"])
    row = adjust(db, employee=employee)

    with as_app_user(db, USER_DIRECTOR):
        db.execute(
            "update retro_adjustments set cancelled_at = now() where id = %s", (row,)
        )
    # Политика `only_from_trigger` — `using`, поэтому строка просто не видна
    # правке: изменено ноль строк, отмена не проставлена.
    assert db.execute(
        "select cancelled_at from retro_adjustments where id = %s", (row,)
    ).fetchone()[0] is None


def test_the_amount_of_a_transfer_cannot_be_rewritten(db):
    """Правится только отмена: переписанная сумма врала бы правдоподобно."""
    june, (employee, _) = approved_with(db, ["retro-rewrite"])
    row = adjust(db, employee=employee, amount="500.00")

    error = rejected(db, "update retro_adjustments set amount = 9999 where id = %s", (row,))
    assert "правится только отмена" in str(error)
    assert db.execute(
        "select amount from retro_adjustments where id = %s", (row,)
    ).fetchone()[0] == Decimal("500.00")


def test_recalculating_the_source_cancels_its_transfers(db):
    """Пересчёт источника отменяет перенос — иначе разница посчиталась бы дважды."""
    june, (employee, _) = approved_with(db, ["retro-cancel"])
    row = adjust(db, employee=employee)

    set_status(db, june, "reopened")
    db.execute(
        "update payruns set status = 'calculated', calculated_at = now() where id = %s",
        (june,),
    )

    cancelled, reason = db.execute(
        "select cancelled_at, cancelled_reason from retro_adjustments where id = %s", (row,)
    ).fetchone()
    assert cancelled is not None, "перенос пережил пересчёт источника"
    assert "источник пересчитан" in reason


def test_a_transfer_is_not_cancelled_by_an_approval(db):
    """Утверждение — не пересчёт: разница остаётся жить."""
    june = payrun_in(db, "calculated")
    db.execute("update payruns set calculated_at = now() where id = %s", (june,))
    employee, _ = make_slip(db, june, "retro-keep")
    row = adjust(db, employee=employee)

    set_status(db, june, "approved")
    assert db.execute(
        "select cancelled_at from retro_adjustments where id = %s", (row,)
    ).fetchone()[0] is None


def test_reopening_is_refused_when_the_delta_is_already_approved(db):
    """Разница выплачена у получателя — источник заново не открывается.

    Держит триггер, а не приложение: проверено на **владельце схемы**, которого
    политики не ограничивают вовсе.
    """
    june, (employee, _) = approved_with(db, ["retro-locked"])
    july = payrun_in(db, "approved", period=JULY)
    adjust(db, employee=employee, source=JUNE, target=JULY)

    db.execute("select set_config('app.transition_reason', 'проверка', true)")
    error = rejected(db, "update payruns set status = 'reopened' where id = %s", (june,))
    assert "уже перенесена в утверждённый период" in str(error)
    assert status_of(db, june) == "approved"
    assert status_of(db, july) == "approved"


def test_reopening_is_allowed_while_the_delta_is_not_approved(db):
    """Пока получатель не утверждён, откат свободен — обратимость не отбирается."""
    june, (employee, _) = approved_with(db, ["retro-open"])
    payrun_in(db, "calculated", period=JULY)
    adjust(db, employee=employee, source=JUNE, target=JULY)

    set_status(db, june, "reopened")
    assert status_of(db, june) == "reopened"


def test_a_cancelled_transfer_does_not_lock_the_source(db):
    """Отменённый перенос ничего не держит: разницы у получателя больше нет."""
    june, (employee, _) = approved_with(db, ["retro-cancelled-lock"])
    july = payrun_in(db, "calculated", period=JULY)
    row = adjust(db, employee=employee, source=JUNE, target=JULY)
    # Отмену ставит только триггер, поэтому и здесь — пересчётом источника.
    set_status(db, june, "reopened")
    db.execute(
        "update payruns set status = 'calculated', calculated_at = now() where id = %s",
        (june,),
    )
    set_status(db, june, "approved")
    set_status(db, july, "approved")

    assert db.execute(
        "select cancelled_at from retro_adjustments where id = %s", (row,)
    ).fetchone()[0] is not None
    assert db.execute("select retro_is_locked(%s, %s)", (T1, JUNE)).fetchone()[0] is False


def test_an_approved_transfer_cannot_be_deleted(db):
    """Выплаченную разницу не стирают. Но неутверждённую убрать можно.

    Полный запрет удаления выглядел бы строже, а на деле повторил бы дефект,
    который в этом блоке ловили дважды: обслуживание не смогло бы убрать за
    собой, и первый же перенос делал бы базу неперезаливаемой.
    """
    june, (employee, _) = approved_with(db, ["retro-delete"])
    payrun_in(db, "approved", period=JULY)
    row = adjust(db, employee=employee, source=JUNE, target=JULY)

    error = rejected(db, "delete from retro_adjustments where id = %s", (row,))
    assert "уже утверждена у получателя" in str(error)

    # Тот же перенос в неутверждённый месяц удаляется без разговоров.
    payrun_in(db, "calculated", period=AUGUST)
    other = adjust(db, employee=employee, source=JUNE, target=AUGUST)
    db.execute("delete from retro_adjustments where id = %s", (other,))
    assert db.execute(
        "select 1 from retro_adjustments where id = %s", (other,)
    ).fetchall() == []


def test_the_transfer_goes_away_with_its_employee(db):
    """Каскад исполняет база, а не Django: перенос уходит вместе с человеком."""
    june, (employee, _) = approved_with(db, ["retro-cascade"])
    row = adjust(db, employee=employee)

    # Утверждённый расчёт база сносить не даёт (T023) — открываем заново.
    set_status(db, june, "reopened")
    db.execute("delete from pay_components where tenant_id = %s", (T1,))
    db.execute("delete from payslip_totals where tenant_id = %s", (T1,))
    db.execute("delete from payslips where tenant_id = %s", (T1,))
    db.execute("delete from employees where id = %s", (employee,))
    assert db.execute(
        "select 1 from retro_adjustments where id = %s", (row,)
    ).fetchall() == []


def test_the_setting_is_delta_by_default(db):
    assert db.execute(
        "select retro_mode from tenants where id = %s", (T1,)
    ).fetchone()[0] == "delta"


def test_the_setting_refuses_a_value_it_does_not_know(db):
    """Словарь домена держит база: чужой режим учёта она не примет."""
    error = rejected(db, "update tenants set retro_mode = 'whatever' where id = %s", (T1,))
    assert "retro_mode" in str(error)


# =============================================================================
# 2. Уровень движка: живой Django поверх сида
# =============================================================================


def snapshot(dsn: str, period: str) -> list[tuple]:
    """Построчный слепок всего, что месяц хранит. Сверяется целиком.

    Не «итог не изменился»: перестановка сумм между людьми оставила бы итог
    прежним, и проверка прошла бы на переписанном месяце.
    """
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """select e.external_id, c.code, c.ledger, c.amount, c.retro_source_period,
                      t.net, t.gross, t.tax, t.contributions, t.total_cost,
                      p.status, p.calculated_at, s.notes
                 from pay_components c
                 join payslips s on s.id = c.payslip_id
                 join employees e on e.id = s.employee_id
                 join payruns p on p.id = s.payrun_id
                 left join payslip_totals t on t.payslip_id = s.id
                where p.period = %s
                order by e.external_id, c.code, c.ledger, c.amount""",
            (period,),
        ).fetchall()


@pytest.fixture
def june_approved(web_env):
    """Посчитанный и утверждённый июнь на данных сида — исходная точка задачи."""
    wipe_payruns(web_env)

    from core.models import Payrun, Tenant
    from payrun import calc, lifecycle
    from web.dbcontext import db_context

    tenant = Tenant.objects.get(code="rs-dev")
    with db_context(user_id=None):
        pass
    calc.calculate_period(
        tenant_id=tenant.id, period=date(2026, 6, 1),
        visible_ledgers=["official", "supplementary", "internal"],
    )
    payrun = Payrun.objects.get(tenant=tenant, period=date(2026, 6, 1))
    lifecycle.approve(payrun, actor_id=None)

    # Ставки запоминаются до правки: тесты этого модуля правят их задним числом,
    # а база у веб-тестов общая на процесс. Не вернуть их — значит испортить
    # суммы всем соседним модулям, и упадут они не здесь.
    from core.models import EmploymentTerm, Period, Timesheet

    rates = dict(EmploymentTerm.objects.values_list("id", "base_rate"))
    try:
        yield tenant
    finally:
        for term_id, rate in rates.items():
            EmploymentTerm.objects.filter(pk=term_id).update(base_rate=rate)
        wipe_payruns(web_env)
        # Всё, что заведено ради получателя, тоже убирается. Список периодов
        # отсортирован по убыванию месяца, поэтому оставленный июль становится
        # «первым периодом» для каждого теста, который берёт первую ссылку, —
        # и ломает их молча, вдалеке отсюда.
        Timesheet.objects.filter(period__gt=date(2026, 6, 1)).delete()
        Period.objects.filter(tenant=tenant, period__gt=date(2026, 6, 1)).delete()


def bump_rates(factor: str) -> None:
    """Правка задним числом: ставки закрытого месяца изменились."""
    from core.models import EmploymentTerm

    for term in EmploymentTerm.objects.all():
        term.base_rate = term.base_rate * Decimal(factor)
        term.save(update_fields=["base_rate"])


def test_the_closed_period_stays_byte_for_byte_the_same(web_env, june_approved):
    """ГЛАВНАЯ ПРОВЕРКА ЗАДАЧИ: закрытый месяц не сдвинулся ни на копейку.

    Ставки правятся задним числом, разница переносится вперёд — и построчный
    слепок июня сверяется целиком, вместе со статусом, временем расчёта и
    примечаниями строк.
    """
    from payrun import retro

    before = snapshot(web_env, "2026-06-01")
    assert before, "июнь не посчитан — проверять нечего"

    bump_rates("2")
    found = retro.drift(june_approved.id, date(2026, 6, 1))
    assert found, "правка ставок не дала расхождения — проверять нечего"

    target, moved = retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )
    assert target == date(2026, 7, 1)
    assert moved.total != 0

    assert snapshot(web_env, "2026-06-01") == before, "закрытый период изменился"


def test_the_delta_appears_in_the_current_period_marked_with_its_source(web_env, june_approved):
    """Разница видна в текущем месяце и объясняет, за какой месяц она пришла."""
    from core.models import PayComponent, Timesheet
    from payrun import calc, retro

    bump_rates("2")
    retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )

    # У июля должны быть свои табели, иначе считать нечего.
    for sheet in Timesheet.objects.filter(period=date(2026, 6, 1)):
        sheet.pk = None
        sheet.id = None
        sheet.period = date(2026, 7, 1)
        sheet.save()

    calc.calculate_period(
        tenant_id=june_approved.id, period=date(2026, 7, 1),
        visible_ledgers=["official", "supplementary", "internal"],
    )
    carried = PayComponent.objects.filter(
        payslip__payrun__period=date(2026, 7, 1),
        retro_source_period=date(2026, 6, 1),
    )
    assert carried.exists(), "разница не доехала до текущего периода"


def test_a_second_transfer_does_not_double_the_first(web_env, june_approved):
    """Накопление: после переноса расхождения нет, а вторая правка едет одна."""
    from payrun import retro

    bump_rates("2")
    _, first = retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )
    assert not retro.drift(june_approved.id, date(2026, 6, 1)), (
        "сразу после переноса расхождение осталось — вторая попытка задвоила бы"
    )

    bump_rates("2")
    _, second = retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )
    assert not retro.drift(june_approved.id, date(2026, 6, 1))

    # Два переноса вместе равны **одному** расхождению между тем, что июнь
    # хранит, и тем, что он даёт сейчас. Сумма считается независимой дорогой,
    # без вычитания уже перенесённого: иначе проверка повторяла бы саму
    # реализацию. Без накопления вторая разница включила бы первую ещё раз, и
    # сумма оказалась бы больше.
    fresh, error = retro._fresh(june_approved.id, date(2026, 6, 1))
    assert not error
    stored = retro._stored(june_approved.id, date(2026, 6, 1))
    once = sum(
        (body["amount"] for body in fresh.values()), Decimal(0)
    ) - sum((body["amount"] for body in stored.values()), Decimal(0))
    assert first.total + second.total == once


def test_nothing_to_transfer_is_refused_with_words(web_env, june_approved):
    from payrun import retro
    from payrun.errors import PayrunRefused

    with pytest.raises(PayrunRefused) as caught:
        retro.post(
            tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
            visible_ledgers=["official", "supplementary", "internal"],
        )
    assert "переносить нечего" in caught.value.message


def test_an_open_period_is_not_transferred_from(web_env, june_approved):
    """Из открытого месяца переносить нечего: его правят и пересчитывают."""
    from core.models import Payrun
    from payrun import lifecycle, retro
    from payrun.errors import PayrunRefused

    payrun = Payrun.objects.get(tenant=june_approved, period=date(2026, 6, 1))
    lifecycle.reopen(payrun, reason="проверка")

    bump_rates("2")
    with pytest.raises(PayrunRefused) as caught:
        retro.post(
            tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
            visible_ledgers=["official", "supplementary", "internal"],
        )
    assert "только из закрытого месяца" in caught.value.message


def test_the_setting_switches_the_product_to_recalculation(web_env, june_approved):
    """Переключатель — настройка тенанта, и она действительно решает."""
    from payrun import retro
    from payrun.errors import PayrunRefused

    june_approved.retro_mode = retro.RECALCULATE
    june_approved.save(update_fields=["retro_mode"])
    bump_rates("2")
    try:
        with pytest.raises(PayrunRefused) as caught:
            retro.post(
                tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
                visible_ledgers=["official", "supplementary", "internal"],
            )
        assert "пересчётом, а не переносом" in caught.value.message
    finally:
        june_approved.retro_mode = retro.DELTA
        june_approved.save(update_fields=["retro_mode"])


def test_the_accountant_cannot_transfer_a_hidden_ledger(web_env, june_approved):
    """Переносить может тот, кто видит все затронутые регистры — как и считать."""
    from payrun import retro
    from payrun.errors import LedgerAccessDenied

    bump_rates("2")
    with pytest.raises(LedgerAccessDenied) as caught:
        retro.post(
            tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
            visible_ledgers=["official"],
        )
    assert "supplementary" in caught.value.ledgers


def test_the_target_is_the_first_period_that_is_not_approved(web_env, june_approved):
    """Утверждённый месяц принять разницу не может — она едет дальше."""
    from core.models import Payrun
    from payrun import lifecycle, retro

    july = Payrun.objects.create(tenant=june_approved, period=date(2026, 7, 1))
    Payrun.objects.filter(pk=july.pk).update(status="calculated")
    july.refresh_from_db()
    lifecycle.approve(july, actor_id=None)

    assert retro.next_open_period(june_approved.id, date(2026, 6, 1)) == date(2026, 8, 1)


def test_the_year_rolls_over(web_env, june_approved):
    from payrun import retro

    assert retro.next_open_period(june_approved.id, date(2026, 12, 1)) == date(2027, 1, 1)


# =============================================================================
# 3. Экран: что видит человек
# =============================================================================


def alert_of(html: str) -> str:
    """Только плашка отказа, а не вся страница.

    Проверка «где-то в HTML есть нужные слова» здесь бесполезна: страница и так
    объясняет человеку каждый недоступный ему запрет (T072), поэтому текст про
    отсутствие права стоит на ней всегда — и с выброшенной проверкой тоже.
    Подтверждено порчей: до этого сужения тест был зелёным по чужой причине.
    """
    import re

    found = re.search(r'<div class="alert">(.*?)</div>', html, re.S)
    return found.group(1) if found else ""


def page_of(client, month: date, tenant) -> str:
    """Адрес страницы нужного месяца.

    Именно нужного, а не первого в списке: база у веб-тестов общая на процесс,
    и соседний тест успевает завести свои периоды. Тест, берущий «первую
    ссылку», проверял бы то, что попалось.
    """
    from core.models import Period

    period, _ = Period.objects.get_or_create(tenant=tenant, period=month)
    return f"/periods/{period.id}/"


def carry_forward(tenant, source: date, target: date) -> None:
    """Перенести разницу и довести её до ведомости получателя.

    У получателя должны быть свои табели, иначе считать нечего: разница едет в
    рабочий месяц, а не в пустоту.
    """
    from core.models import Timesheet
    from payrun import calc, retro

    retro.post(
        tenant_id=tenant.id, source=source, actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )
    # База у веб-тестов общая на процесс, поэтому табели получателя могли
    # остаться от соседнего теста: заводим только недостающие.
    if not Timesheet.objects.filter(period=target).exists():
        for sheet in Timesheet.objects.filter(period=source):
            sheet.pk = None
            sheet.id = None
            sheet.period = target
            sheet.save()
    calc.calculate_period(
        tenant_id=tenant.id, period=target,
        visible_ledgers=["official", "supplementary", "internal"],
    )


def test_the_page_offers_the_transfer_and_names_the_target(client, web_env, june_approved):
    from payrun import retro

    bump_rates("2")
    login_as(client, "director")
    html = body(client.get(page_of(client, date(2026, 6, 1), june_approved)))
    assert "Перенести разницу" in html
    assert retro.month_title(date(2026, 7, 1)) in html


def test_the_transfer_needs_the_right_on_the_page(client, web_env, june_approved):
    """У роли без права кнопки нет, а её адрес отвечает 403 теми же словами."""
    bump_rates("2")
    login_as(client, "manager")
    url = page_of(client, date(2026, 6, 1), june_approved)
    html = body(client.get(url))
    assert "Перенести разницу" not in html

    answer = client.post(url + "retro/")
    assert answer.status_code == 403
    # Отказ ищется внутри плашки: та же фраза стоит на странице всегда, потому
    # что страница объясняет каждый недоступный запрет (T072).
    assert "Перенос разницы за закрытый месяц не входит в права" in alert_of(body(answer))


def test_the_sheet_says_the_row_is_a_delta(client, web_env, june_approved):
    """Строка разницы обязана объяснять себя: иначе это непонятная сумма."""
    from payrun import retro

    bump_rates("2")
    carry_forward(june_approved, date(2026, 6, 1), date(2026, 7, 1))

    login_as(client, "director")
    page = body(client.get(page_of(client, date(2026, 7, 1), june_approved)))
    assert "Перерасчёт за" in page, "строка разницы себя не объясняет"
    assert retro.month_title(date(2026, 6, 1)) in page


def test_the_page_says_the_transfer_is_not_shown_yet(client, web_env, june_approved):
    """Перенесли, но не пересчитали — страница обязана сказать это вслух."""
    from payrun import retro

    bump_rates("2")
    retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official", "supplementary", "internal"],
    )

    login_as(client, "director")
    html = body(client.get(page_of(client, date(2026, 7, 1), june_approved)))
    assert "пересчитайте период" in html.lower()


def test_the_transfer_from_the_page_leaves_the_closed_month_alone(client, web_env, june_approved):
    """Тот же снимок, но пройденный настоящим нажатием со страницы."""
    before = snapshot(web_env, "2026-06-01")
    bump_rates("2")

    login_as(client, "director")
    url = page_of(client, date(2026, 6, 1), june_approved)
    answer = client.post(url + "retro/")
    assert answer.status_code == 302

    assert snapshot(web_env, "2026-06-01") == before


# =============================================================================
# 3a. Расхождение считается в срезе роли, а не «база минус свежий расчёт» (T085)
# =============================================================================
#
# Что было. `drift` сравнивал сохранённое (его отдаёт база **в срезе роли**) со
# свежим расчётом (он считается движком **целиком**, мимо видимости регистров).
# Разница двух несопоставимых величин равна ровно объёму невидимых роли строк,
# поэтому бухгалтеру показывали чужие фамилии, суммы и названия регистров — и
# при этом утверждали, что закрытый месяц разошёлся, хотя не менялось ничего.
#
# Проверяется обеими половинами сразу: в срезе не менялось — блока нет вовсе;
# в срезе менялось — блок есть и показывает **только** свой срез. Без второй
# половины первая доказывалась бы выключенной проверкой.


def role_ledgers(code: str) -> list[str]:
    """Регистры роли — из сида, а не списком в тесте.

    Список наизусть разошёлся бы с сидом молча, и тест проверял бы память
    автора вместо продукта.
    """
    from web.auth import DEV_USERS

    return list(DEV_USERS[code].ledgers)


def as_role(code: str):
    """Смотреть на данные глазами роли — ролью `app_user`, как ходит продукт.

    Владелец схемы политики обходит, поэтому «в срезе роли» можно проверить
    только под контекстом пользователя: именно так уже прожил незамеченным один
    дефект видимости регистров.
    """
    from web.auth import DEV_USERS
    from web.dbcontext import db_context

    return db_context(DEV_USERS[code].user_id)


@pytest.mark.parametrize("role", ["director", "accountant", "manager"])
def test_a_month_that_did_not_change_has_no_drift_at_all(web_env, june_approved, role):
    """Ничего не правили — расхождения нет ни у одной роли.

    Ровно тот случай, на котором дефект и виден: у директора блока не было
    никогда, а бухгалтеру и управляющему показывали «расхождение» размером в
    невидимую им часть ведомости.
    """
    from payrun import retro

    with as_role(role):
        found = retro.drift(
            june_approved.id, date(2026, 6, 1), visible_ledgers=role_ledgers(role)
        )

    assert not found.error, found.error
    assert found.lines == [], f"{role}: расхождение там, где ничего не менялось"


def test_the_accountant_sees_exactly_the_official_part_of_the_drift(web_env, june_approved):
    """Правка задела все регистры — бухгалтер видит свой и ровно его.

    Сумма сверяется с официальной частью расхождения директора: «показали не
    всё» проверяется равенством, а не тем, что чужих слов на экране нет.
    """
    from payrun import retro

    bump_rates("2")

    with as_role("accountant"):
        mine = retro.drift(
            june_approved.id, date(2026, 6, 1), visible_ledgers=role_ledgers("accountant")
        )
    with as_role("director"):
        whole = retro.drift(
            june_approved.id, date(2026, 6, 1), visible_ledgers=role_ledgers("director")
        )

    assert mine.lines, "правка ставок обязана задеть и официальный регистр"
    assert {line.ledger for line in mine.lines} == {"official"}
    assert {line.ledger for line in whole.lines} > {"official"}, (
        "у директора расхождение шире одного регистра — иначе сравнивать нечего"
    )

    official_of_whole = sum(
        (line.amount for line in whole.lines if line.ledger == "official"), Decimal(0)
    )
    assert mine.total == official_of_whole
    assert mine.total != whole.total


def test_the_page_of_an_unchanged_month_shows_no_drift_to_anyone(
    client, web_env, june_approved
):
    """Экран, ролью `app_user`: блока расхождений нет ни у кого."""
    for role in ("director", "accountant", "manager"):
        login_as(client, role)
        html = body(client.get(page_of(client, date(2026, 6, 1), june_approved)))
        assert "изменились после утверждения" not in html, (
            f"{role}: страница утверждает расхождение там, где ничего не менялось"
        )


def test_the_page_shows_the_accountant_only_his_own_slice(client, web_env, june_approved):
    """Правка была — блок есть, и в нём ни одной чужой строки.

    Чужого регистра не должно быть ни строкой, ни словом (D023): проверяется и
    отсутствие названий, и совпадение итога с официальной частью.
    """
    from payrun import retro
    from web.format import money

    bump_rates("2")
    login_as(client, "accountant")
    html = body(client.get(page_of(client, date(2026, 6, 1), june_approved)))

    assert "изменились после утверждения" in html, "своё расхождение показать обязаны"
    assert "Дополнительный" not in html
    assert "Внутренний" not in html

    with as_role("accountant"):
        mine = retro.drift(
            june_approved.id, date(2026, 6, 1), visible_ledgers=role_ledgers("accountant")
        )
    assert money(mine.total) in html, "на экране не та сумма, что даёт срез роли"


def test_the_transfer_moves_only_what_the_role_can_see(web_env, june_approved):
    """Перенос уносит свой срез и не отказывается из-за чужого.

    Прежнее поведение — отказ «разница попадает в регистры, недоступные вашей
    роли» с их перечислением — само было сообщением о существовании чужого
    регистра. Теперь роль переносит своё, а чужое остаётся тому, кто его видит.
    """
    from core.models import RetroAdjustment
    from payrun import retro

    bump_rates("2")
    target, moved = retro.post(
        tenant_id=june_approved.id, source=date(2026, 6, 1), actor_id=None,
        visible_ledgers=["official"],
    )

    assert target == date(2026, 7, 1)
    assert {line.ledger for line in moved.lines} == {"official"}
    assert set(
        RetroAdjustment.objects.filter(source_period=date(2026, 6, 1))
        .values_list("ledger", flat=True)
    ) == {"official"}

    # Чужая часть никуда не делась: её перенесёт тот, кому она видна.
    rest = retro.drift(
        june_approved.id, date(2026, 6, 1),
        visible_ledgers=["official", "supplementary", "internal"],
    )
    assert {line.ledger for line in rest.lines} == {"supplementary", "internal"}


# =============================================================================
# 4. Обслуживание: база с перенесённой разницей должна перезаливаться
# =============================================================================


def test_the_seed_runs_over_a_transferred_and_approved_delta():
    """Сид обязан убрать за собой базу, где разница уже перенесена и утверждена.

    Найдено уборкой за смоуком, а не чтением кода: `seed_dev` падал с
    «разница за этот месяц уже перенесена в утверждённый период». Уборка
    открывает утверждённые расчёты **одним оператором**, а порядок строк в нём
    не задан: если июнь попадался раньше июля, сторож честно отказывал.

    Это третий случай одного и того же класса в этом блоке (issue #60 и #62):
    новый сторож правильных чисел ломает обслуживание, потому что обслуживание
    ходит теми же путями записи, что и человек. Сторожа не ослабляются — уборка
    идёт от позднего месяца к раннему.
    """
    psycopg = pytest.importorskip("psycopg")

    from conftest import run_manage, temp_database

    with temp_database("payrun_retro_seed") as dsn:
        run_manage(dsn, "seed_dev")

        with psycopg.connect(dsn, autocommit=True) as conn:
            tenant = conn.execute("select id from tenants limit 1").fetchone()[0]
            employee = conn.execute("select id from employees limit 1").fetchone()[0]
            for period in (JUNE, JULY):
                payrun = conn.execute(
                    "insert into payruns (tenant_id, period) values (%s, %s) returning id",
                    (tenant, period),
                ).fetchone()[0]
                set_status(conn, payrun, "calculated", "approved")
            conn.execute(
                """insert into retro_adjustments
                       (tenant_id, source_period, target_period, employee_id,
                        code, title, amount, ledger)
                   values (%s, %s, %s, %s, 'hours.regular', 'Часы', 100, 'official')""",
                (tenant, JUNE, JULY, employee),
            )
            assert conn.execute(
                "select retro_is_locked(%s, %s)", (tenant, JUNE)
            ).fetchone()[0] is True, "подготовка не воспроизвела случай"

        run_manage(dsn, "seed_dev")

        with psycopg.connect(dsn) as conn:
            assert conn.execute("select count(*) from payruns").fetchone()[0] == 0
            assert conn.execute(
                "select count(*) from retro_adjustments"
            ).fetchone()[0] == 0
