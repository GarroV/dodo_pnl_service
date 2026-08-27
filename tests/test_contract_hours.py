"""Договорные часы: добрал ли человек норму по договору (issue #171).

Эталон (модуль 1 «Табель») показывает в сетке две колонки — **«Договор»** и
**«Δ к договору»**, — а в итогах точки три числа: лишние часы, недостающие и
сальдо. Всё это считается от величины, которой в продукте не было вовсе:
сколько часов человек обязан отработать по договору.

Зачем это управляющему. В конце месяца он открывает табель ради одного вопроса:
сходятся ли часы с договорами. Без договорной величины ответить нечем — видно
только «сколько отработал», а «сколько должен» держится в голове. Сальдо по
точке отвечает на тот же вопрос сразу по смене целиком: людей добрали или
недобрали, и на сколько.

Почему величина живёт в условиях найма, а не в справочнике должностей.
Договорные часы меняются вместе с условиями — перевели человека на полставки,
и его норма по договору стала другой с той же даты. В должности лежит
умолчание, которое подставляется при найме; дальше величина живёт версией, как
ставка.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as

JUNE = date(2026, 6, 1)


@pytest.fixture
def with_contract_hours(web_env):
    """Человеку сида проставлены договорные часы, остальным нет."""
    from core.models import EmploymentTerm, Timesheet

    row = (
        Timesheet.objects.select_related("employee")
        .filter(period=JUNE)
        .order_by("employee__external_id")
        .first()
    )
    term = (
        EmploymentTerm.objects.filter(employee_id=row.employee_id)
        .order_by("valid_from")
        .last()
    )
    before = term.contract_hours
    EmploymentTerm.objects.filter(pk=term.pk).update(contract_hours=Decimal("160.00"))
    yield row
    EmploymentTerm.objects.filter(pk=term.pk).update(contract_hours=before)


def test_the_terms_hold_the_contract_hours(web_env):
    """Величина есть у условий найма — значит версионируется вместе с ними."""
    from core.models import EmploymentTerm

    field = EmploymentTerm._meta.get_field("contract_hours")
    assert field.null, "договорных часов может не быть: у части людей их не считают"


def test_the_grid_shows_the_contract_and_the_difference(client, with_contract_hours):
    """В сетке видно, сколько человек должен и на сколько разошлось."""
    login_as(client, "director")
    html = body(client.get(f"/timesheets/{_period_id()}/"))

    assert "Договор" in html or "Contract" in html, "колонки договорных часов нет"
    assert "Δ" in html or "разниц" in html.lower(), "колонки расхождения нет"
    assert "160" in html, "договорная величина не показана"


def test_the_unit_total_says_extra_and_missing(client, with_contract_hours):
    """Итог точки отвечает на вопрос смены целиком: добрали или недобрали."""
    login_as(client, "director")
    html = body(client.get(f"/timesheets/{_period_id()}/"))

    lowered = html.lower()
    assert "лишн" in lowered or "extra" in lowered, "лишних часов в итогах нет"
    assert "недоста" in lowered or "missing" in lowered, "недостающих часов в итогах нет"
    assert "сальдо" in lowered or "balance" in lowered, "сальдо по точке нет"


def test_a_person_without_the_contract_is_not_counted_as_missing(client, web_env):
    """У кого договорных часов нет — тот не «недобрал», а просто не считается.

    Иначе итог точки заявлял бы недостачу по всем, кому величину не завели, — и
    первое же открытие табеля показывало бы тревогу на ровном месте.
    """
    from core.models import Period
    from timesheets.grid import build_grid

    period = Period.objects.get(period=JUNE)
    grid = build_grid(period.tenant_id, period.period)
    without = [row for row in grid.rows if row.contract_hours is None]
    assert without, "тест проверяет не то: договорные часы есть у всех"
    assert all(row.contract_diff is None for row in without)
    assert grid.contract_missing == Decimal("0")


def test_the_difference_is_signed(client, with_contract_hours):
    """Δ показывает сторону: переработал — плюс, недоработал — минус."""
    from core.models import Period
    from timesheets.grid import build_grid

    period = Period.objects.get(period=JUNE)
    grid = build_grid(period.tenant_id, period.period)
    row = next(r for r in grid.rows if r.contract_hours is not None)

    assert row.contract_diff == row.total - row.contract_hours
    # Сальдо точки — сумма расхождений, а не разность двух итогов: человек без
    # договорных часов не должен утягивать её в минус.
    assert grid.contract_balance == grid.contract_extra - grid.contract_missing


def _period_id() -> str:
    from core.models import Period

    return str(Period.objects.get(period=JUNE).id)
