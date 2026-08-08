"""Табель: выплата наличными и ручная корректировка со следом (T051, issue #43).

Дыра, ради которой это заведено: движок принимает `cash_payout` и
`manual_correction` (в таблице партнёра это `ISPLATA U KES` и
`KOREKCIJA DO MINIMALCA`), а в схеме соответствующих колонок не было. Следствия
были молчаливые: сид терял значения, `payslips.to_cash` всегда оставался нулём,
а вносить ручную правку через интерфейс было некуда.

Отдельное требование — **след** (D025): к любой сумме можно дойти до того, кто
и почему её поправил. Поэтому корректировка без причины и без автора
отвергается базой, а не проверяется в форме: писать в табель будут и импорт,
и интерфейс, и фоновая задача.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import T1, body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"
AUTHOR = "d1111111-0000-0000-0000-000000000001"


def _timesheet(conn, **extra) -> str:
    """Табель в тестовом тенанте. Всё, что не задано, — как в обычной вставке."""
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Тестов') returning id""",
        (T1, f"ts-{len(extra)}-{extra.get('external', '')}"),
    ).fetchone()[0]
    columns = ["tenant_id", "employee_id", "period", "norm_hours"]
    values = [T1, employee, JUNE, 176]
    for name, value in extra.items():
        if name == "external":
            continue
        columns.append(name)
        values.append(value)
    placeholders = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"insert into timesheets ({', '.join(columns)}) values ({placeholders}) returning id",
        values,
    ).fetchone()[0]


# --- схема -------------------------------------------------------------------


def test_cash_payout_defaults_to_zero(db):
    """Обычный табель ничего не знает про наличные — и это ноль, а не null."""
    ts = _timesheet(db, external="plain")
    row = db.execute(
        "select cash_payout, manual_correction from timesheets where id = %s", (ts,)
    ).fetchone()
    assert row == (Decimal("0.00"), None)


def test_correction_with_a_trace_is_accepted(db):
    ts = _timesheet(
        db, external="ok", manual_correction=Decimal("1200.00"),
        correction_reason="доплата до минималца по письму бухгалтера",
        corrected_by=AUTHOR,
    )
    assert ts is not None


def test_correction_without_a_reason_is_rejected(db):
    """Правка без объяснения через полгода неотличима от ошибки ввода."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        _timesheet(db, external="no-reason", manual_correction=Decimal("1200.00"),
                   corrected_by=AUTHOR)


def test_blank_reason_does_not_pass_for_a_reason(db):
    """Пробел — не причина: иначе проверку обходят одним нажатием клавиши."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        _timesheet(db, external="blank", manual_correction=Decimal("1200.00"),
                   correction_reason="   ", corrected_by=AUTHOR)


def test_correction_without_an_author_is_rejected(db):
    """«Кто» из D025 — такая же часть следа, как «почему»."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        _timesheet(db, external="no-author", manual_correction=Decimal("1200.00"),
                   correction_reason="доплата до минималца")


def test_trace_without_a_correction_is_allowed(db):
    """Обратная сторона не запрещена: причина без правки никому не вредит."""
    _timesheet(db, external="reason-only", correction_reason="просто заметка",
               corrected_by=AUTHOR)


# --- сквозной расчёт ---------------------------------------------------------


@pytest.fixture
def clean_payruns(web_env):
    """Расчёты периода сносятся до и после: тест смотрит на конкретные суммы.

    Табель возвращается к исходному виду тем же движением. Без этого тест ниже
    оставлял в общей базе прогона правку на 1200 и наличные на 5000 навсегда:
    база `web_env` живёт всю сессию, и следующий тест, знающий эталонную сумму
    расчёта, получал 1 955 603,13 вместо 1 951 806,13 — падая не по своей вине.
    Найдено при T020: тест импорта зелёный поодиночке и красный в общем прогоне.
    """
    import psycopg

    def snapshot():
        with psycopg.connect(web_env) as conn:
            return conn.execute(
                """select id, manual_correction, correction_reason, corrected_by,
                          corrected_at, cash_payout
                     from timesheets where period = %s order by id""",
                (JUNE,),
            ).fetchall()

    before = snapshot()
    wipe_payruns(web_env)
    yield web_env
    wipe_payruns(web_env)
    with psycopg.connect(web_env, autocommit=True) as conn:
        for row in before:
            conn.execute(
                """update timesheets
                      set manual_correction = %s, correction_reason = %s,
                          corrected_by = %s, corrected_at = %s, cash_payout = %s
                    where id = %s""",
                (*row[1:], row[0]),
            )


def _seeded_employee(dsn: str) -> tuple[str, str]:
    """Кто-нибудь из сида с табелем за июнь: id и внешний ключ."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(
            """select e.id, e.external_id
                 from timesheets t join employees e on e.id = t.employee_id
                where t.period = %s
                order by e.external_id limit 1""",
            (JUNE,),
        ).fetchone()


def test_calculation_applies_the_correction_and_the_cash_payout(client, clean_payruns):
    """Главное требование задачи: правка доходит до ведомости, наличные — до to_cash."""
    import psycopg

    employee_id, external_id = _seeded_employee(clean_payruns)
    with psycopg.connect(clean_payruns, autocommit=True) as conn:
        conn.execute(
            """update timesheets
                  set manual_correction = 1200.00,
                      correction_reason = 'доплата до минималца по письму бухгалтера',
                      corrected_by = %s,
                      cash_payout = 5000.00
                where employee_id = %s and period = %s""",
            (AUTHOR, employee_id, JUNE),
        )

    login_as(client, "director")
    client.post(period_url(client) + "calculate/", follow=True)

    with psycopg.connect(clean_payruns) as conn:
        component = conn.execute(
            """select c.amount from pay_components c
                 join payslips p on p.id = c.payslip_id
                where p.employee_id = %s and c.code = 'manual_correction'""",
            (employee_id,),
        ).fetchone()
        slip = conn.execute(
            """select t.to_cash, p.notes
                 from payslips p join payslip_totals t on t.payslip_id = p.id
                where p.employee_id = %s""",
            (employee_id,),
        ).fetchone()

    assert component == (Decimal("1200.00"),), "корректировка не дошла до компонентов"
    to_cash, notes = slip
    assert to_cash == Decimal("5000.00"), "выплата наличными не попала в to_cash"
    assert any("корректировка" in note for note in notes), f"нет пометки в notes: {notes}"

    # И то же самое видно на экране — иначе поправить и не заметить проще, чем кажется.
    assert "Ручная корректировка" in body(client.get(period_url(client)))
    assert external_id  # внешний ключ существует: сид не пустой
