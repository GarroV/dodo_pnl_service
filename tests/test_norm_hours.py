"""Что делает норма часов и чего она не делает (T082).

Наблюдение, с которого всё началось: норму часов подняли **всем** сотрудникам,
строка табеля изменилась, а итог расчёта не сдвинулся ни на копейку. Выглядит
как молчаливо игнорируемый вход, и это худший из возможных видов дефекта —
число вводят, а оно никуда не идёт.

Разбор показал другое: норма табеля читается движком ровно в одном месте — там,
где правило схемы прямо говорит брать часы из неё (`worked_hours_source:
half_of_norm`, полставка без введённых часов). Во всех остальных схемах деньги
считаются из **введённых часов**, а пропорции надбавок и необлагаемого минимума
— из константы страны `reference_norm_hours`, а не из строки табеля. Значит
норма не игнорируется: у неё узкая и объявленная роль.

Тесты ниже закрепляют обе половины: где норма не двигает ничего (и почему это
правильно) и где двигает (иначе первая половина доказывала бы, что вход мёртв).

**Что осталось вопросом бухгалтеру, а не решением этих тестов.** Пропорции
считаются от `reference_norm_hours` = 176, а не от нормы месяца из
производственного календаря. В июне 2026 они совпадают, поэтому разницы не
видно ни на одном числе; в месяце с другой нормой они разойдутся молча. Прав ли
здесь календарь или константа — предмет проверки по таблице бухгалтерии за
второй месяц, которой у нас нет. Вопрос заведён отдельно (issue #74), а не
решён здесь.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import wipe_payruns

D = Decimal

# Все схемы страны: если норма где-то и участвует в формуле, это видно только
# перебором — по одной схеме такое не ловится.
SCHEMES = ["standard", "half_time", "half_time_min_base", "temporary", "direct"]

JUNE = date(2026, 6, 1)
JUNE_TOTAL = D("1951806.13")  # ориентир приёмки: 60 строк ведомости директора


def worker(preset, scheme: str):
    from payroll import Employee

    return Employee(
        ext_id="test", name="Тест Тестович", group="office", scheme=scheme,
        base_rate=D(500), coefficient=D(1),
    )


def sheet(norm, **hours):
    from payroll import Timesheet

    return Timesheet(
        hours={k: D(v) for k, v in hours.items()},
        insured_hours=D(sum(hours.values())), norm_hours=D(norm),
    )


def calculated(preset, scheme: str, ts):
    from payroll import PayrollEngine

    return PayrollEngine(preset).calculate(worker(preset, scheme), ts)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_the_norm_does_not_move_a_row_whose_hours_are_entered(serbia_preset, scheme):
    """Часы введены — норма не участвует в деньгах ни в одной схеме.

    Так и задумано: платят за отработанное, а пропорции надбавок и
    необлагаемого минимума считаются от константы страны. Проверяется по всем
    схемам сразу, потому что «в этой не влияет» ничего не говорит о соседней.
    """
    plain = calculated(serbia_preset, scheme, sheet(176, regular=176))
    raised = calculated(serbia_preset, scheme, sheet(200, regular=176))

    assert raised.net == plain.net
    assert raised.gross == plain.gross
    assert raised.contributions == plain.contributions
    assert [c.amount for c in raised.components] == [c.amount for c in plain.components]


def test_the_norm_is_the_source_of_hours_where_the_rule_says_so(serbia_preset):
    """Полставка без введённых часов: норма — единственный источник часов.

    Без этой проверки первая половина доказывала бы, что вход мёртв. Правило
    названо прямо: `worked_hours_source: half_of_norm` у схемы `half_time`.
    """
    assert serbia_preset["schemes"]["half_time"]["worked_hours_source"] == "half_of_norm"

    plain = calculated(serbia_preset, "half_time", sheet(176))
    raised = calculated(serbia_preset, "half_time", sheet(200))

    assert plain.net == D("44750.00")
    assert raised.net > plain.net, (
        "норма перестала быть источником часов у полставки — "
        "тогда вводить её на этой схеме стало нечем"
    )


def test_entered_hours_win_over_the_norm(serbia_preset):
    """Отработанное важнее нормы даже там, где норма — источник часов.

    Приняли или уволили человека посреди месяца — платят за фактические часы, а
    не за половину нормы. Условие стоит в движке, и без проверки его однажды
    упростят до «половина нормы всегда».
    """
    from_norm = calculated(serbia_preset, "half_time", sheet(176))
    entered = calculated(serbia_preset, "half_time", sheet(176, regular=40))

    assert entered.net != from_norm.net
    assert calculated(serbia_preset, "half_time", sheet(200, regular=40)).net == entered.net


def test_proration_comes_from_the_country_constant_not_from_the_timesheet(serbia_preset):
    """Пропорции считаются от `reference_norm_hours`, и это видно на числах.

    Тест держит границу: подъём нормы в табеле надбавку не двигает, а правка
    константы страны — двигает. Если однажды кто-то решит, что пропорцию надо
    брать из табеля, здесь станет красно, а не молча иначе.
    """
    from payroll.presets import apply_overrides

    bonus = "meal_and_vacation_bonus"
    half = sheet(176, regular=88)

    plain = calculated(serbia_preset, "standard", half)
    by_timesheet_norm = calculated(serbia_preset, "standard", sheet(352, regular=88))
    other_country_norm = calculated(
        apply_overrides(serbia_preset, {"constants.reference_norm_hours": 352}),
        "standard", half,
    )

    amount = {c.code: c.amount for c in plain.components}[bonus]
    assert {c.code: c.amount for c in by_timesheet_norm.components}[bonus] == amount
    assert {c.code: c.amount for c in other_country_norm.components}[bonus] != amount


def test_the_month_calendar_is_not_an_input_of_the_calculation(web_env):
    """Норма месяца из производственного календаря в расчёт не входит вовсе.

    Это не мнение, а свойство, которое надо знать: календарь виден на странице
    периода и правится в справочнике, поэтому выглядит как вход расчёта. Он им
    не является — пропорции берутся из правил страны. Совпадают ли эти два
    числа в месяце, где норма не 176, — вопрос бухгалтеру (issue #74, рядом с
    Q003), и решать его тестом нельзя.
    """
    from django.db.models import Sum

    from core.models import Calendar, PayComponent
    from payrun.calc import calculate_period

    tenant_id = _tenant()
    wipe_payruns(web_env)
    before = calculate_period(
        tenant_id=tenant_id, period=JUNE, visible_ledgers=ALL_LEDGERS
    )
    assert _total(PayComponent, Sum, before) == JUNE_TOTAL, (
        "материал теста собран не про тот случай: расчёт разошёлся с приёмкой"
    )

    month = Calendar.objects.get(country_code="RS", period=JUNE)
    was = month.norm_hours
    Calendar.objects.filter(pk=month.pk).update(norm_hours=was + 24)
    try:
        wipe_payruns(web_env)
        after = calculate_period(
            tenant_id=tenant_id, period=JUNE, visible_ledgers=ALL_LEDGERS
        )
        assert _total(PayComponent, Sum, after) == JUNE_TOTAL
    finally:
        Calendar.objects.filter(pk=month.pk).update(norm_hours=was)


def test_the_norm_of_every_timesheet_does_not_move_the_period(web_env, period_restored):
    """То самое наблюдение, слово в слово: подняли норму всем — итог не сдвинулся.

    Проверяется на живых данных сида и на контрольном числе приёмки, потому что
    проверять его на одном выдуманном человеке было бы не тем же самым: в июне
    есть и полставочники, и сдельные, и временные работы.
    """
    from django.db.models import F, Sum

    from core.models import PayComponent, Timesheet
    from payrun.calc import calculate_period

    tenant_id = _tenant()
    wipe_payruns(web_env)
    before = calculate_period(
        tenant_id=tenant_id, period=JUNE, visible_ledgers=ALL_LEDGERS
    )
    assert _total(PayComponent, Sum, before) == JUNE_TOTAL

    moved = Timesheet.objects.filter(period=JUNE).update(norm_hours=F("norm_hours") + 24)
    assert moved > 0, "материал теста собран не про тот случай: табелей нет"

    wipe_payruns(web_env)
    after = calculate_period(tenant_id=tenant_id, period=JUNE, visible_ledgers=ALL_LEDGERS)
    assert _total(PayComponent, Sum, after) == JUNE_TOTAL, (
        "норма табеля сдвинула итог месяца — значит она входит в формулы, "
        "и разбор T082 устарел"
    )


ALL_LEDGERS = ["official", "supplementary", "internal"]


def _tenant():
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev").id


def _total(model, sum_, outcome):
    return model.objects.filter(payslip__payrun_id=outcome.payrun_id).aggregate(
        total=sum_("amount")
    )["total"]
