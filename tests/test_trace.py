"""
След расчёта: какие правила сработали, с какими входами, какой версией ставки
(T013, D025).

Смысл следа — объяснимость: к любой сумме можно дойти до входных часов и до
версии правила, по которой она посчитана. Поэтому главная проверка здесь не
«поля заполнены», а **сумма шагов равна итогу строки**: след, из которого не
складывается число, объясняет не тот расчёт.

Экран следа (T029) — не этот блок. Здесь проверяются данные, которые он получит.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet, d
from payroll.trace import explain

JUNE = date(2026, 6, 1)
MIN_RATE = Decimal("371")


def employee(scheme="standard", group="kitchen", rate=MIN_RATE, coef=1) -> Employee:
    return Employee(
        ext_id="test", name="Тест Тестович", group=group,
        scheme=scheme, base_rate=d(rate), coefficient=d(coef),
    )


def timesheet(**hours) -> Timesheet:
    insured = d(hours.pop("insured", 176))
    return Timesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=insured, norm_hours=insured,
    )


def net_steps(trace):
    return [s for s in trace if s.contributes_to == "net"]


# --- главный критерий: след складывается в итог -------------------------------


def test_trace_steps_add_up_to_net(engine, serbia_preset, sample_rows):
    """По всем 32 людям набора и всем четырём схемам."""
    mismatches = []
    for row in sample_rows:
        slip = engine.calculate(row.employee, row.timesheet)
        trace = explain(row.employee, row.timesheet, serbia_preset)
        total = sum((s.applied_value for s in net_steps(trace)), Decimal(0))
        if total != slip.net:
            mismatches.append(f"{row.sheet} / {row.name}: след {total:.2f}, нето {slip.net:.2f}")
    assert not mismatches, "след не складывается в нето:\n  " + "\n  ".join(mismatches)


def test_the_sum_check_is_not_decorative(engine, serbia_preset):
    """Потеряли шаг — проверка обязана это заметить, иначе она ничего не стоит."""
    e, ts = employee(), timesheet(regular=176)
    slip = engine.calculate(e, ts)
    trace = net_steps(explain(e, ts, serbia_preset))
    assert len(trace) > 1, "на одном шаге проверка бессмысленна"
    without_one = sum((s.applied_value for s in trace[:-1]), Decimal(0))
    assert without_one != slip.net


def test_every_component_has_a_step_behind_it(engine, serbia_preset, sample_rows):
    """След и ведомость — об одном и том же расчёте, а не о двух разных."""
    for row in sample_rows:
        slip = engine.calculate(row.employee, row.timesheet)
        trace = explain(row.employee, row.timesheet, serbia_preset)
        assert [c.code for c in slip.components] == [s.rule_code for s in net_steps(trace)]
        for component, step in zip(slip.components, net_steps(trace), strict=True):
            assert component.amount == step.applied_value


# --- входы, по которым сумму можно повторить руками ---------------------------


def test_hour_step_shows_hours_rate_and_percent(serbia_preset):
    """Больничный: 371 × 0,65 × 20 — три числа, из которых собирается сумма."""
    step = next(
        s for s in explain(employee(coef=2), timesheet(regular=156, sick=20), serbia_preset)
        if s.rule_code == "hours.sick"
    )
    assert step.input_values["hours"] == 20
    assert step.input_values["rate"] == MIN_RATE * 2
    assert step.input_values["pay_percent"] == Decimal("0.65")
    inputs = step.input_values
    assert inputs["rate"] * inputs["pay_percent"] * inputs["hours"] == step.applied_value


def test_minimum_guarantee_step_shows_what_it_topped_up_to(serbia_preset):
    """Откуда взялись 2597: 371 − 371×0,65, двадцать часов."""
    step = next(
        s for s in explain(employee(), timesheet(regular=32, sick=20), serbia_preset)
        if s.rule_code == "minimum_guarantee"
    )
    assert step.input_values["floor"] == MIN_RATE
    assert step.input_values["hour_types"] == ["sick"]
    assert step.applied_value == MIN_RATE * Decimal("0.35") * 20


def test_allowance_step_names_the_way_it_was_prorated(serbia_preset):
    """Топли оброк по дням или по часам — то, что у бухгалтера под вопросом (Q003)."""
    by_days = next(
        s for s in explain(employee(), timesheet(regular=88), serbia_preset)
        if s.rule_code == "meal_and_vacation_bonus"
    )
    by_hours = next(
        s for s in explain(employee(scheme="half_time"), timesheet(regular=20), serbia_preset)
        if s.rule_code == "meal_and_vacation_bonus"
    )
    assert by_days.input_values["prorate_by"] == "worked_days"
    assert by_hours.input_values["prorate_by"] == "worked_hours"


def test_derived_totals_are_explained_too(engine, serbia_preset):
    """Бруто и взносы — не компоненты, но объяснить их надо так же."""
    e, ts = employee(), timesheet(regular=176)
    slip = engine.calculate(e, ts)
    trace = explain(e, ts, serbia_preset)
    derived = {s.contributes_to: s for s in trace if s.contributes_to != "net"}

    assert derived["gross"].applied_value == slip.gross
    assert derived["contributions"].applied_value == slip.contributions
    assert derived["total_cost"].applied_value == slip.total_cost
    assert derived["gross"].input_values["method"] == "net_minus_prorated_allowance"
    assert derived["contributions"].input_values["method"] == "employer_plus_withheld"


def test_scheme_with_its_own_tax_rule_explains_the_tax(serbia_preset):
    """У схемы с минимальной базой налог считается отдельно — он тоже в следе."""
    trace = explain(employee(scheme="half_time_min_base"), timesheet(regular=88), serbia_preset)
    assert any(s.contributes_to == "tax" for s in trace)


def test_manual_correction_is_marked_as_input_not_as_a_rule(serbia_preset):
    """Правка руками — не правило: выдавать её за сработавшее правило нельзя."""
    ts = timesheet(regular=32, sick=20)
    ts.manual_correction = d(1000)
    step = next(s for s in explain(employee(), ts, serbia_preset)
                if s.rule_code == "manual_correction")
    assert step.source_level == "input"
    assert step.rule_version_id is None


# --- версия правила: откуда приехало значение ---------------------------------


def test_trace_of_a_file_preset_has_no_version(serbia_preset):
    """Пресет из файла версии не имеет — и врать про неё нельзя."""
    trace = explain(employee(), timesheet(regular=176), serbia_preset)
    assert all(s.rule_version_id is None for s in trace)
    assert {s.source_level for s in net_steps(trace)} == {"country"}


def test_trace_names_the_version_of_the_rule_that_applied(web_env):
    """Правило переопределено партнёром — след обязан показать чьё и какое."""
    from django.db import transaction

    from core.models import RuleOverride, RulePreset, Tenant
    from core.rules import load_preset_at

    atomic = transaction.atomic()
    atomic.__enter__()
    try:
        tenant = Tenant.objects.get(code="rs-dev")
        row = RuleOverride.objects.create(
            tenant=tenant, scope_type="tenant", scope_id=None,
            path="hour_types.sick.pay_percent", value=0.8, valid_from=date(2020, 1, 1),
        )
        preset = load_preset_at(tenant.id, "RS", JUNE)
        trace = explain(employee(coef=2), timesheet(regular=156, sick=20), preset)

        sick = next(s for s in trace if s.rule_code == "hours.sick")
        assert sick.source_level == "tenant"
        assert sick.rule_version_id == row.id
        assert sick.input_values["pay_percent"] == Decimal("0.8")

        holiday_source = next(s for s in trace if s.rule_code.startswith("hours.regular"))
        assert holiday_source.source_level == "country"
        assert holiday_source.rule_version_id == RulePreset.objects.get(code="serbia-2026").id
    finally:
        transaction.set_rollback(True)
        atomic.__exit__(None, None, None)


def test_trace_survives_a_scheme_override(serbia_preset):
    """Пресет пересобран — след обязан остаться согласованным с суммами."""
    from payroll.presets import apply_overrides

    tweaked = apply_overrides(serbia_preset, {"hour_types.sick.pay_percent": 0.5})
    e, ts = employee(), timesheet(regular=100, sick=20)
    slip = PayrollEngine(tweaked).calculate(e, ts)
    total = sum((s.applied_value for s in net_steps(explain(e, ts, tweaked))), Decimal(0))
    assert total == slip.net


def test_unknown_scheme_still_fails_loudly(serbia_preset):
    with pytest.raises(KeyError):
        explain(employee(scheme="не-существует"), timesheet(regular=176), serbia_preset)
