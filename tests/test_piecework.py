"""Сдельная работа курьеров: часы и сдельная величина как два равноправных пути.

Основание — D032, ответ владельца на Q011: чем меряют работу курьера в Сербии,
бухгалтер ещё не сказал, поэтому поддержаны оба способа, а выбор — правило
группы `work_measure`, а не ветка в коде.

Что здесь проверяется и почему именно это:

1. **Умолчание не сдвинулось.** Без `work_measure` и без сдельной величины
   расчёт обязан быть побайтово прежним: на движке стоит сходимость с
   бухгалтерией, и «поддержали второй способ» не должно стоить ни копейки на
   первом.
2. **Оба способа дают разные и объяснимые числа.** Разные — иначе поддержка
   второго пути ничего не значит. Объяснимые — в следе расчёта видно
   количество, ставку и то, каким правилом выбран способ.
3. **Способ — версионируемое правило.** Переключение с новой даты не трогает
   уже посчитанный месяц. Это то же требование, что в T026, и проверяется оно
   на живой базе (`test_timesheets.py` и ниже по файлу), а не только на чистом
   движке.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from payroll import Employee, PayrollEngine, Timesheet
from payroll.presets import apply_overrides

D = Decimal


def courier(scheme: str = "direct") -> Employee:
    return Employee(
        ext_id="c-1", name="Курир Марко", group="couriers", scheme=scheme,
        base_rate=D("420.00"), coefficient=D("1.0"),
    )


def amounts(slip) -> dict[str, Decimal]:
    return {c.code: c.amount for c in slip.components}


# =============================================================================
# 1. Умолчание: ничего не сдвинулось
# =============================================================================


def test_default_measure_is_hours(engine):
    """Группа без `work_measure` считается по часам — как считалась всегда."""
    slip = engine.calculate(courier(), Timesheet(hours={"regular": D(168)}))

    assert amounts(slip) == {"hours.regular": D("420.00") * D(168)}
    assert slip.net == D("420.00") * D(168)


def test_piece_value_without_piecework_measure_changes_nothing(engine):
    """Сдельная величина у почасовой группы денег не даёт.

    Иначе переключение способа было бы необратимым: величина, введённая по
    ошибке, начала бы участвовать в расчёте той группы, где её не спрашивали.
    """
    with_piece = engine.calculate(
        courier(), Timesheet(hours={"regular": D(168)}, piece_value=D(500)),
    )
    without = engine.calculate(courier(), Timesheet(hours={"regular": D(168)}))

    assert amounts(with_piece) == amounts(without)
    assert with_piece.net == without.net


# =============================================================================
# 2. Сдельно: количество × ставка и фиксированная сумма
# =============================================================================


def piecework_engine(preset, measure: str) -> PayrollEngine:
    return PayrollEngine(
        apply_overrides(preset, {"groups.couriers.work_measure": measure})
    )


def test_deliveries_pay_quantity_times_rate(serbia_preset):
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    assert amounts(slip) == {"piecework.deliveries": D("420.00") * D(120)}
    assert slip.net == D("50400.00")
    # Схема `direct`: бруто равно нето, взносов нет — мера работы этого не меняет.
    assert slip.gross == slip.net
    assert slip.contributions == D(0)
    assert slip.total_cost == slip.net


def test_fixed_amount_is_the_amount_itself(serbia_preset):
    """У фиксированной выплаты ставка не применяется вовсе.

    Величина в табеле — сама сумма. Умножить её на ставку значило бы получить
    правдоподобное число в сотни раз больше настоящего.
    """
    engine = piecework_engine(serbia_preset, "fixed_amount")

    slip = engine.calculate(courier(), Timesheet(piece_value=D("45000.00")))

    assert amounts(slip) == {"piecework.fixed_amount": D("45000.00")}
    assert slip.net == D("45000.00")


def test_two_measures_give_different_numbers(serbia_preset):
    """Оба способа на одних и тех же данных дают разное — иначе выбирать нечего."""
    ts = lambda: Timesheet(hours={"regular": D(168)}, piece_value=D(120))  # noqa: E731

    by_hours = PayrollEngine(serbia_preset).calculate(courier(), ts())
    by_pieces = piecework_engine(serbia_preset, "deliveries").calculate(courier(), ts())
    by_fixed = piecework_engine(serbia_preset, "fixed_amount").calculate(courier(), ts())

    assert by_hours.net == D("70560.00")   # 168 ч × 420
    assert by_pieces.net == D("50400.00")  # 120 доставок × 420
    assert by_fixed.net == D("120.00")     # сама величина
    assert len({by_hours.net, by_pieces.net, by_fixed.net}) == 3


def test_hours_of_a_piecework_group_are_not_paid_and_not_silent(serbia_preset):
    """Часы сдельной группы не оплачиваются — и об этом сказано, а не умолчано.

    Молчание здесь было бы дорогим: человек видит в табеле 168 часов и ждёт за
    них денег, а их нет. Пометка уходит в ведомость (`payslips.notes`).
    """
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(
        courier(), Timesheet(hours={"regular": D(168)}, piece_value=D(120)),
    )

    assert amounts(slip) == {"piecework.deliveries": D("50400.00")}
    assert any("сдельно" in note for note in slip.notes), slip.notes


def test_no_note_when_there_are_no_hours(serbia_preset):
    """Пометка появляется от часов, а не от самого факта сдельной оплаты."""
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    assert slip.notes == []


def test_zero_piece_value_gives_no_component(serbia_preset):
    """Ноль — это отсутствие начисления, а не компонент на ноль."""
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(0)))

    assert slip.components == []
    assert slip.net == D(0)


def test_unknown_measure_is_loud(serbia_preset):
    """Опечатка в правиле — громкий отказ, а не расчёт мимо неё.

    Тихо посчитать по часам значило бы выдать правдоподобное неверное число:
    правило в базе заведено, в списке видно, а расчёт идёт мимо него.
    """
    engine = piecework_engine(serbia_preset, "per_delivery")

    with pytest.raises(ValueError, match="per_delivery"):
        engine.calculate(courier(), Timesheet(piece_value=D(120)))


# =============================================================================
# 3. След расчёта: из чего собрано число
# =============================================================================


def test_trace_explains_the_piecework_amount(serbia_preset):
    engine = piecework_engine(serbia_preset, "deliveries")

    slip = engine.calculate(courier(), Timesheet(piece_value=D(120)))

    step = next(s for s in slip.trace if s.rule_code == "piecework.deliveries")
    # Путь — туда, где сделан ВЫБОР способа: именно его переопределяет партнёр,
    # и именно его версию надо показать в следе.
    assert step.rule_code_path == "groups.couriers.work_measure"
    assert step.input_values["measure"] == "deliveries"
    assert step.input_values["quantity"] == D(120)
    assert step.input_values["rate"] == D("420.00")
    assert step.input_values["pay_per_unit"] is True
    assert step.applied_value == D("50400.00")


def test_trace_of_fixed_amount_does_not_pretend_there_is_a_rate(serbia_preset):
    engine = piecework_engine(serbia_preset, "fixed_amount")

    slip = engine.calculate(courier(), Timesheet(piece_value=D("45000.00")))

    step = next(s for s in slip.trace if s.rule_code == "piecework.fixed_amount")
    assert step.input_values["pay_per_unit"] is False
    assert step.input_values["rate"] is None


# =============================================================================
# 4. Живая база: ввод величины, права и закрытый период
# =============================================================================
#
# Дальше идёт то, ради чего задача и заведена: способ оплаты — версионируемое
# правило, а не свойство «навсегда». Переключение способа не имеет права
# сдвинуть уже посчитанный месяц — это то же требование, что в T026.

from contextlib import contextmanager  # noqa: E402
from datetime import date  # noqa: E402

from conftest import as_app_user, body, login_as  # noqa: E402

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
ALL_LEDGERS = ["official", "supplementary", "internal"]

COURIERS_MEASURE = "groups.couriers.work_measure"


@contextmanager
def measure_switched(tenant_id, measure: str, *, since: date):
    """Переключить способ оплаты курьеров с указанной даты — и вернуть обратно.

    Тем же механизмом, которым партнёр меняет любое другое правило: строка
    `rule_overrides` с путём, значением и датой начала действия. Отдельной
    «настройки способа оплаты» в продукте нет намеренно — иначе одно и то же
    правило имело бы два места жительства и две разные истории версий.
    """
    from core.models import RuleOverride

    row = RuleOverride.objects.create(
        tenant_id=tenant_id, scope_type="tenant", scope_id=None,
        path=COURIERS_MEASURE, value=measure, valid_from=since,
    )
    try:
        yield row
    finally:
        row.delete()


def courier_row(period=JUNE):
    """Строка табеля курьера в сиде разработки."""
    from core.models import EmploymentTerm
    from core.models import Timesheet as Row

    ids = EmploymentTerm.objects.filter(group__code="couriers").values_list(
        "employee_id", flat=True
    )
    row = (
        Row.objects.filter(period=period, employee_id__in=list(ids))
        .select_related("employee")
        .order_by("employee__external_id")
        .first()
    )
    assert row is not None, "в сиде нет ни одного курьера — тест бессмысленен"
    return row


def grid_url(client) -> str:
    import re

    from conftest import period_url

    match = re.search(r"([0-9a-f-]{36})", period_url(client))
    return f"/timesheets/{match.group(1)}/"


def test_piece_column_appears_only_for_piecework_groups(client, web_env):
    """Колонка сдельной величины показывается, когда есть кому её вводить."""
    login_as(client, "director")
    url = grid_url(client)

    assert "timesheet-piece" not in body(client.get(url))
    assert 'name="piece"' not in body(client.get(url))

    row = courier_row()
    with measure_switched(row.tenant_id, "deliveries", since=JUNE):
        html = body(client.get(url))

    assert 'name="piece"' in html
    # Подпись способа стоит у ячейки: 120 доставок и 120 динаров — разные деньги.
    assert "Доставки" in html


def test_piece_value_is_saved_and_survives_reload(client, web_env):
    """Ввёл сдельную величину, ушёл со страницы, вернулся — число на месте."""
    from core.models import Timesheet as Row

    login_as(client, "director")
    url = grid_url(client)
    row = courier_row()

    with measure_switched(row.tenant_id, "deliveries", since=JUNE):
        response = client.post(f"{url}piece/", {"row": str(row.pk), "piece": "120"})
        assert response.status_code == 200
        assert response["X-Cell-Value"] == "120.00"

        assert Row.objects.get(pk=row.pk).piece_value == Decimal("120.00")
        assert 'value="120.00"' in body(client.get(url))

    Row.objects.filter(pk=row.pk).update(piece_value=0)


def test_garbage_in_the_piece_cell_is_refused_out_loud(client, web_env):
    from core.models import Timesheet as Row

    login_as(client, "director")
    url = grid_url(client)
    row = courier_row()

    with measure_switched(row.tenant_id, "deliveries", since=JUNE):
        response = client.post(f"{url}piece/", {"row": str(row.pk), "piece": "восемь"})

    assert response.status_code == 422
    assert "не число" in response.content.decode()
    # Отказ обязан ничего не менять и вернуть то, что осталось в базе.
    assert response["X-Cell-Value"] == "0.00"
    assert Row.objects.get(pk=row.pk).piece_value == Decimal(0)


def test_negative_piece_value_is_refused(client, web_env):
    login_as(client, "director")
    url = grid_url(client)
    row = courier_row()

    response = client.post(f"{url}piece/", {"row": str(row.pk), "piece": "-5"})

    assert response.status_code == 422
    assert "отрицательной" in response.content.decode()


def test_manager_cannot_write_piece_value_of_another_unit(client, web_env):
    """Чужая точка и несуществующая строка выглядят одинаково — как и у часов."""
    from core.models import Timesheet as Row
    from core.models import Unit

    login_as(client, "manager")
    url = grid_url(client)
    ns1 = Unit.objects.filter(code="NS1").values_list("id", flat=True).first()
    stranger = Row.objects.filter(period=JUNE).exclude(unit_id=ns1).first()

    response = client.post(f"{url}piece/", {"row": str(stranger.pk), "piece": "10"})

    assert response.status_code == 404
    assert Row.objects.get(pk=stranger.pk).piece_value == Decimal(0)


def test_app_user_cannot_read_piece_value_of_another_tenant(db):
    """Колонка живёт в той же строке табеля, что и часы, — и закрыта так же.

    Проверяется ролью `app_user`: владелец таблиц политики обходит, и проверка
    им была бы зелёной всегда.
    """
    from conftest import T1, U_BG1, USER_OTHER

    employee_id = db.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, 'piece-1', 'Курир', 'Тестов') returning id""",
        (T1,),
    ).fetchone()[0]
    db.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period,
                                   norm_hours, hours, piece_value)
           values (%s, %s, %s, %s, 176, '{}'::jsonb, 120)""",
        (T1, employee_id, U_BG1, JUNE),
    )

    with as_app_user(db, USER_OTHER) as conn:
        seen = conn.execute(
            "select piece_value from timesheets where employee_id = %s", (employee_id,)
        ).fetchall()

    assert seen == [], "сдельная величина чужого партнёра видна"


def test_closed_unit_refuses_the_piece_value_in_the_database(db):
    """Закрытие точки (T022) накрывает и сдельную величину, а не только часы.

    Проверяется в базе и ролью `app_user`: обещание «объясняет приложение,
    гарантирует база» должно держаться и для новой колонки. Владелец таблиц
    политики обходит, и проверка им была бы зелёной при любой дыре.
    """
    import psycopg

    from conftest import T1, U_NS1, USER_MANAGER

    denied = psycopg.errors.InsufficientPrivilege
    employee_id = db.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, 'piece-closed', 'Курир', 'Закрытов') returning id""",
        (T1,),
    ).fetchone()[0]
    sheet = db.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period,
                                   norm_hours, hours, piece_value)
           values (%s, %s, %s, %s, 176, '{}'::jsonb, 120) returning id""",
        (T1, employee_id, U_NS1, JUNE),
    ).fetchone()[0]
    db.execute(
        """insert into timesheet_closures (tenant_id, unit_id, period, closed_by)
           values (%s, %s, %s, %s)""",
        (T1, U_NS1, JUNE, USER_MANAGER),
    )

    with as_app_user(db, USER_MANAGER) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(denied):
            conn.execute(
                "update timesheets set piece_value = 999 where id = %s", (sheet,)
            )
        conn.execute("rollback to savepoint attempt")

    assert db.execute(
        "select piece_value from timesheets where id = %s", (sheet,)
    ).fetchone()[0] == Decimal("120.00")


def test_closed_unit_refuses_the_piece_value_on_screen_and_says_why(client, web_env):
    """И то же самое экраном: отказ с объяснением и с числом, которое в базе."""
    import psycopg

    from conftest import USER_MANAGER
    from core.models import Timesheet as Row
    from core.models import Unit

    login_as(client, "director")
    url = grid_url(client)
    row = courier_row()
    Row.objects.filter(pk=row.pk).update(piece_value=Decimal("120.00"))
    unit_code = Unit.objects.filter(pk=row.unit_id).values_list("code", flat=True).first()

    with psycopg.connect(web_env, autocommit=True) as conn:
        conn.execute(
            """insert into timesheet_closures (tenant_id, unit_id, period, closed_by)
               values (%s, %s, %s, %s)""",
            (str(row.tenant_id), str(row.unit_id), JUNE, USER_MANAGER),
        )
    try:
        with measure_switched(row.tenant_id, "deliveries", since=JUNE):
            response = client.post(f"{url}piece/", {"row": str(row.pk), "piece": "500"})

        assert response.status_code == 409
        assert unit_code in response.content.decode()
        # Экран обязан вернуться к тому, что осталось в базе (T066).
        assert response["X-Cell-Value"] == "120.00"
        assert Row.objects.get(pk=row.pk).piece_value == Decimal("120.00")
    finally:
        with psycopg.connect(web_env, autocommit=True) as conn:
            conn.execute(
                "delete from timesheet_closures where tenant_id = %s and period = %s",
                (str(row.tenant_id), JUNE),
            )
        Row.objects.filter(pk=row.pk).update(piece_value=0)


def test_a_typo_in_the_measure_refuses_the_run_by_name(web_env):
    """Опечатка в способе оплаты — отказ с именами, а не 500 из середины счёта.

    Движок на неизвестной мере поднимает `ValueError` — и это правильно: тихо
    посчитать по часам он не имеет права. Но человеку, который переопределил
    правило, `ValueError` посреди счёта показывается как «Server Error», а
    исправлять правило ему. Поэтому мера проверяется до счёта и называет людей,
    ровно как схема расчёта и база для взносов.
    """
    from payrun.calc import compute
    from payrun.errors import PayrunRefused

    row = courier_row()

    with measure_switched(row.tenant_id, "per_delivery", since=JUNE):
        with pytest.raises(PayrunRefused) as refusal:
            compute(row.tenant_id, JUNE)

    assert "per_delivery" in refusal.value.message
    assert row.employee.external_id in refusal.value.details


def test_switching_the_measure_does_not_move_a_closed_period(web_env):
    """Главный тест задачи: переключение способа не ломает закрытый месяц.

    Способ оплаты — версионируемое правило: он начинает действовать с даты, а не
    задним числом. Уже посчитанный и утверждённый июнь обязан остаться прежним
    до копейки — и в базе, и при повторном счёте по сегодняшним правилам.
    """
    from conftest import wipe_payruns
    from core.models import PayComponent, Payrun
    from core.models import Timesheet as Row
    from payrun.calc import calculate_period, compute
    from payrun.lifecycle import approve

    wipe_payruns(web_env)
    row = courier_row()
    Row.objects.filter(pk=row.pk).update(piece_value=Decimal("120.00"))
    try:
        outcome = calculate_period(
            tenant_id=row.tenant_id, period=JUNE, visible_ledgers=ALL_LEDGERS
        )
        before = sorted(
            (str(c.payslip_id), c.code, str(c.amount))
            for c in PayComponent.objects.filter(payslip__payrun_id=outcome.payrun_id)
        )
        approve(Payrun.objects.get(pk=outcome.payrun_id), actor_id=None)

        # Партнёр переключает курьеров на сдельную оплату с ИЮЛЯ.
        with measure_switched(row.tenant_id, "deliveries", since=JULY):
            after = sorted(
                (str(c.payslip_id), c.code, str(c.amount))
                for c in PayComponent.objects.filter(payslip__payrun_id=outcome.payrun_id)
            )
            assert after == before, "закрытый месяц переписан сменой способа оплаты"

            # И пересчёт по сегодняшним правилам дал бы июню ровно то же самое:
            # правило начало действовать позже, чем этот месяц.
            _, slips = compute(row.tenant_id, JUNE)
            fresh = sorted(
                (case.external_id, component.code, f"{component.amount:.2f}")
                for case, slip in slips for component in slip.components
            )
            stored = sorted(
                (c.payslip.employee.external_id, c.code, f"{c.amount:.2f}")
                for c in PayComponent.objects.filter(
                    payslip__payrun_id=outcome.payrun_id
                ).select_related("payslip__employee")
            )
            assert fresh == stored
    finally:
        Row.objects.filter(pk=row.pk).update(piece_value=0)
        wipe_payruns(web_env)


def test_the_same_switch_from_june_does_change_june(web_env):
    """Обратная сторона: правило, начавшее действовать в июне, июнь меняет.

    Без этой проверки предыдущий тест доказывал бы не «версионирование
    работает», а «переключатель ни на что не влияет».
    """
    from conftest import wipe_payruns
    from core.models import Timesheet as Row
    from payrun.calc import compute

    wipe_payruns(web_env)
    row = courier_row()
    Row.objects.filter(pk=row.pk).update(piece_value=Decimal("120.00"))
    try:
        def nets():
            _, slips = compute(row.tenant_id, JUNE)
            return {
                case.external_id: slip.net for case, slip in slips
                if case.employee_id == row.employee_id
            }

        by_hours = nets()
        with measure_switched(row.tenant_id, "deliveries", since=JUNE):
            by_pieces = nets()

        assert by_hours != by_pieces, "переключатель способа ничего не меняет"
        # 120 доставок × ставка курьера — число, которое можно проверить руками.
        from core.models import EmploymentTerm

        term = (
            EmploymentTerm.objects.filter(employee_id=row.employee_id)
            .order_by("-valid_from").first()
        )
        expected = Decimal("120") * term.base_rate * term.coefficient
        assert list(by_pieces.values())[0] == expected
    finally:
        Row.objects.filter(pk=row.pk).update(piece_value=0)
        wipe_payruns(web_env)
