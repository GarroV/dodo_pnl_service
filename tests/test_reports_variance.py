"""Отчёт расхождений «период против прошлого» с порогами на компонент (T030).

Что проверяется и почему именно так.

**Подброшенное отклонение обязано найтись.** Это приёмка задачи, поэтому
главная проверка — не «отчёт открывается», а: берём посчитанный месяц, кладём
рядом прошлый с намеренно изменённой суммой и смотрим, показал ли экран именно
её. Проверка бессмысленна без второй половины: изменение **в пределах порога**
показываться не должно, иначе «находит всё» и «находит нужное» неотличимы.

**Порог — на компонент, а не один общий.** Надбавка в 100 динаров и оклад в
100 тысяч не сравниваются одной меркой: 5% нормально для сверхурочных и
ненормально для оклада. Поэтому у каждого компонента свои процент и абсолютный
пол, и берутся они из **конфигурации** (пресет страны, версионированный и
переопределяемый партнёром), а не из констант в коде.

**Единица сравнения — сотрудник × компонент.** Отклонение живёт там: правка по
одному человеку растворяется в итоге компонента по тридцати людям и не находится
вовсе.

**Ни строк, ни следа** (D023). Обе стороны сравнения собираются тем же
`reports.sheet`, что и ведомость, то есть из сумм, уже отобранных политиками
базы. «Сумма изменилась на X», где X посчитан по всем регистрам, — утечка.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = date(2026, 6, 1)
MAY = date(2026, 5, 1)


def cells(*rows):
    """Материал сравнения: `(сотрудник, регистр, код, сумма)`."""
    from payrun.sheet import Cell

    return [
        Cell(employee=name, unit="BG1", ledger=ledger, code=code,
             title=code, amount=Decimal(amount), key=name)
        for name, ledger, code, amount in rows
    ]


THRESHOLDS = {
    "default": {"percent": 10, "absolute": 500},
    "components": {
        "hours.regular": {"percent": 5, "absolute": 1000},
        "meal_and_vacation_bonus": {"percent": 1, "absolute": 1},
    },
}


# --- пороги: конфигурация, а не константа ------------------------------------


def test_thresholds_come_from_the_preset_and_differ_per_component():
    """Разные компоненты меряются разной меркой — это и есть требование задачи."""
    from reports.variance import thresholds_from

    rules = thresholds_from(THRESHOLDS)
    assert rules.for_code("hours.regular").percent == Decimal(5)
    assert rules.for_code("meal_and_vacation_bonus").percent == Decimal(1)
    # Компонента в настройке нет — берётся умолчание страны, а не ноль и не
    # бесконечность: молчаливое «пропустим» и молчаливое «покажем всё» одинаково
    # бесполезны.
    assert rules.for_code("нет-такого").percent == Decimal(10)


def test_a_preset_without_thresholds_refuses_instead_of_inventing_them():
    """Порогов нет — отчёт отказывается словами, а не берёт число из воздуха.

    Константа в коде выглядела бы на экране ровно как настроенный порог, и
    отличить «партнёр так решил» от «мы так зашили» было бы нечем.
    """
    from reports.variance import ThresholdsMissing, thresholds_from

    with pytest.raises(ThresholdsMissing):
        thresholds_from(None)
    with pytest.raises(ThresholdsMissing):
        thresholds_from({"components": {}})


# --- сравнение ---------------------------------------------------------------


def test_a_planted_deviation_is_reported():
    """Приёмка T030 в самой прямой форме."""
    from reports.variance import compare

    previous = cells(("Иванов", "official", "hours.regular", "50000"))
    current = cells(("Иванов", "official", "hours.regular", "58000"))

    report = compare(current, previous, thresholds=THRESHOLDS)

    assert len(report.lines) == 1
    line = report.lines[0]
    assert line.employee == "Иванов" and line.code == "hours.regular"
    assert line.previous == Decimal("50000") and line.current == Decimal("58000")
    assert line.delta == Decimal("8000")
    assert line.exceeded


def test_a_change_within_the_threshold_is_not_reported():
    """Без этого «находит всё» и «находит нужное» неотличимы."""
    from reports.variance import compare

    previous = cells(("Иванов", "official", "hours.regular", "50000"))
    current = cells(("Иванов", "official", "hours.regular", "50400"))  # +0,8%, +400

    assert compare(current, previous, thresholds=THRESHOLDS).lines == []


def test_both_thresholds_must_be_exceeded_at_once():
    """Процент без абсолютного пола шумит, абсолютный без процента слеп.

    Компонент на 20 динаров, выросший вдвое, — это +20 динаров: показывать его
    рядом с окладом значит утопить настоящее отклонение в мелочи. Оклад в сто
    тысяч, выросший на 1500, перешагнул пол в деньгах — но это 1,5%, то есть
    обычное колебание месяца, а не новость.
    """
    from reports.variance import compare

    loud_percent = compare(
        cells(("Иванов", "official", "hours.regular", "40")),
        cells(("Иванов", "official", "hours.regular", "20")),
        thresholds=THRESHOLDS,
    )
    assert loud_percent.lines == [], "+100%, но всего +20 динаров"

    loud_absolute = compare(
        cells(("Иванов", "official", "hours.regular", "101500")),
        cells(("Иванов", "official", "hours.regular", "100000")),
        thresholds=THRESHOLDS,
    )
    assert loud_absolute.lines == [], "+1500 динаров — выше пола, но всего 1,5%"


def test_a_component_that_appeared_or_vanished_is_a_deviation_too():
    """Ноль → сумма и сумма → ноль: процента нет, но событие есть."""
    from reports.variance import compare

    appeared = compare(
        cells(("Иванов", "official", "hours.sick", "9000")),
        cells(("Иванов", "official", "hours.regular", "50000")),
        thresholds=THRESHOLDS,
    )
    kinds = {(line.code, line.previous, line.current) for line in appeared.lines}
    assert ("hours.sick", Decimal(0), Decimal("9000")) in kinds
    assert ("hours.regular", Decimal("50000"), Decimal(0)) in kinds


def test_each_component_is_measured_by_its_own_threshold():
    """То самое: надбавка и оклад не сравниваются одной меркой."""
    from reports.variance import compare

    previous = cells(
        ("Иванов", "official", "hours.regular", "50000"),
        ("Иванов", "official", "meal_and_vacation_bonus", "1500"),
    )
    current = cells(
        ("Иванов", "official", "hours.regular", "50900"),          # +1,8%, +900
        ("Иванов", "official", "meal_and_vacation_bonus", "1600"),  # +6,7%, +100
    )

    codes = {line.code for line in compare(current, previous, thresholds=THRESHOLDS).lines}
    assert codes == {"meal_and_vacation_bonus"}, (
        "меркой оклада надбавка не измеряется, и наоборот"
    )


def test_deviations_of_one_person_do_not_hide_in_the_total():
    """Единица сравнения — человек, а не компонент по всем сразу.

    Прибавка одному и такая же убавка другому дают нулевой итог компонента.
    Отчёт, считающий по итогам, не увидел бы ничего.
    """
    from reports.variance import compare

    previous = cells(
        ("Иванов", "official", "hours.regular", "50000"),
        ("Петров", "official", "hours.regular", "50000"),
    )
    current = cells(
        ("Иванов", "official", "hours.regular", "58000"),
        ("Петров", "official", "hours.regular", "42000"),
    )

    report = compare(current, previous, thresholds=THRESHOLDS)
    assert {line.employee for line in report.lines} == {"Иванов", "Петров"}
    assert sum((line.delta for line in report.lines), Decimal(0)) == Decimal(0)


def test_a_ledger_cut_narrows_both_sides_the_same_way():
    """Разрез — тот же, что у ведомости: сравниваются сопоставимые срезы."""
    from reports.variance import compare

    previous = cells(
        ("Иванов", "official", "hours.regular", "50000"),
        ("Иванов", "supplementary", "hours.regular", "30000"),
    )
    current = cells(
        ("Иванов", "official", "hours.regular", "58000"),
        ("Иванов", "supplementary", "hours.regular", "38000"),
    )

    whole = compare(current, previous, thresholds=THRESHOLDS)
    cut = compare(current, previous, thresholds=THRESHOLDS, cut="official")

    assert {line.ledger for line in whole.lines} == {"official", "supplementary"}
    assert {line.ledger for line in cut.lines} == {"official"}
    assert cut.total_up == Decimal("8000")
    assert whole.total_up == Decimal("16000")


def test_growth_and_decline_are_summed_apart():
    """T096: взаимно погасившиеся отклонения не складываются в «всё сошлось».

    Отчёт показывал алгебраическую сумму отклонений одним числом. У четырёх
    расхождений выше порога, где прибавка одному равна убавке другому, в
    подвале стояло «Итого отклонений · 4 чел. — 0,00»: над списком настоящих
    расхождений — число, которое читается как «сошлось». Ровно то
    правдоподобно-неверное, что здесь запрещено, только арифметикой.

    Рост и снижение поэтому считаются врозь: у обоих есть смысл поодиночке, а
    у их суммы его нет ни при каких данных.
    """
    from reports.variance import compare

    previous = cells(
        ("Иванов", "official", "hours.regular", "50000"),
        ("Петров", "official", "hours.regular", "50000"),
    )
    current = cells(
        ("Иванов", "official", "hours.regular", "58000"),
        ("Петров", "official", "hours.regular", "42000"),
    )
    report = compare(current, previous, thresholds=THRESHOLDS)

    assert len(report.lines) == 2, "оба отклонения выше порога и должны быть найдены"
    assert report.total_up == Decimal("8000")
    assert report.total_down == Decimal("-8000")
    assert report.grew == 1 and report.fell == 1
    assert not hasattr(report, "total_delta"), (
        "алгебраическая сумма отклонений вернулась: её нельзя показать так, "
        "чтобы она не читалась как итог"
    )


def test_the_report_counts_what_it_compared_not_only_what_it_found():
    """«Отклонений нет» и «сравнивать было нечего» — разные ответы."""
    from reports.variance import compare

    empty = compare([], [], thresholds=THRESHOLDS)
    assert empty.compared == 0 and empty.lines == []

    quiet = compare(
        cells(("Иванов", "official", "hours.regular", "50000")),
        cells(("Иванов", "official", "hours.regular", "50000")),
        thresholds=THRESHOLDS,
    )
    assert quiet.compared == 1 and quiet.lines == []


def test_a_hidden_ledger_never_reaches_the_comparison():
    """Обе стороны приезжают уже отобранными базой — сравнение не расширяет.

    Здесь закреплено, что и сама сборка ничего не добавляет: посчитать «разницу
    по всем регистрам» и показать её тому, кто видит один, — это утечка.
    """
    from reports.variance import compare

    report = compare(
        cells(("Иванов", "official", "hours.regular", "58000")),
        cells(("Иванов", "official", "hours.regular", "50000")),
        thresholds=THRESHOLDS,
    )
    assert {line.ledger for line in report.lines} <= {"official"}


# --- на живых данных, через страницу -----------------------------------------


PLANTED = Decimal("9000.00")     # подброшенное отклонение, заведомо выше порога
NOISE = Decimal("0.50")          # изменение в пределах порога: показываться не должно

# Точка, которой ограничен управляющий в тестовом сиде. Отклонение кладётся
# именно в неё и именно в официальный регистр — это единственный срез, видный
# сразу всем трём ролям проверки ниже.
MANAGER_UNIT = "NS1"


@pytest.fixture
def june_and_planted_may(client, web_env):
    """Посчитанный июнь и прошлый месяц с намеренно изменёнными суммами.

    Май собирается копией июня: сравнивать надо два похожих месяца, иначе
    «отклонение» найдётся в каждой строке и проверка ничего не покажет. Правки
    ровно две — одна заведомо выше порога, вторая заведомо ниже.
    """
    import django

    django.setup()
    from core.models import PayComponent, Payrun, Payslip, Period, Tenant

    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200

    tenant = Tenant.objects.get(code="rs-dev")
    Period.objects.get_or_create(
        tenant=tenant, period=MAY, defaults={"status": "closed"}
    )
    may_run, _ = Payrun.objects.get_or_create(tenant=tenant, period=MAY)
    PayComponent.objects.filter(tenant=tenant, payslip__payrun=may_run).delete()
    Payslip.objects.filter(payrun=may_run).delete()

    june = Payrun.objects.get(tenant=tenant, period=JUNE)
    loud = quiet = None
    # Порядок выборки задан явно. Без него строки приходили в порядке кучи
    # Postgres, а он меняется от прогона к прогону, и подброшенное отклонение
    # попадало то на официальный регистр точки NS1, то на внутренний регистр
    # чужой точки. Во втором случае бухгалтер (видит один регистр) или
    # управляющий (видит одну точку) не видели в отчёте ни одной строки — и
    # проверка «итог роли равен сумме её строк» падала на порядке выборки, а не
    # на дефекте продукта. Плавающий тест хуже отсутствующего: он приучает
    # перезапускать прогон вместо того, чтобы читать падение.
    slips = (
        Payslip.objects.filter(payrun=june)
        .select_related("employee", "unit")
        .order_by("employee__last_name", "employee__first_name", "id")
    )
    for slip in slips:
        twin = Payslip.objects.create(
            tenant=tenant, payrun=may_run, employee_id=slip.employee_id,
            unit_id=slip.unit_id,
        )
        for component in PayComponent.objects.filter(payslip=slip).order_by("code"):
            amount = component.amount
            if (
                loud is None
                and component.code == "hours.regular"
                # Официальный регистр точки NS1 виден всем трём ролям сразу:
                # директору, бухгалтеру (только официальный) и управляющему
                # (только своя точка). Отклонение, видное одной роли, проверяло
                # бы отчёт одной роли.
                and component.ledger == "official"
                and slip.unit is not None
                and slip.unit.code == MANAGER_UNIT
            ):
                # Июнь минус май = +9000 по этому человеку.
                loud = (slip.employee, component.code, component.ledger)
                amount -= PLANTED
            elif quiet is None and component.code == "meal_and_vacation_bonus":
                quiet = (slip.employee, component.code, component.ledger)
                amount -= NOISE
            PayComponent.objects.create(
                tenant=tenant, payslip=twin, code=component.code, title=component.title,
                amount=amount, ledger=component.ledger, channel=component.channel,
                taxable=component.taxable,
            )

    assert loud and quiet, "материал теста собран не из тех компонентов"
    return {"loud": loud, "quiet": quiet}


def variance_url(client) -> str:
    return period_url(client) + "variance/"


def report_rows(html: str) -> list[tuple[str, str, Decimal]]:
    """Строки отчёта глазами человека: сотрудник, компонент, отклонение."""
    found = re.findall(
        r'<tr class="line[^"]*">\s*<td>([^<]+)</td>\s*<td>[^<]*'
        r'<span class="hint">([^<]+)</span>',
        html,
    )
    deltas = re.findall(r'<td class="num delta">([^<]+)</td>', html)
    assert len(found) == len(deltas), "разбор отчёта разъехался с разметкой"
    return [
        (employee.strip(), code.strip(), Decimal(delta.replace(" ", "").replace(",", ".")))
        for (employee, code), delta in zip(found, deltas, strict=True)
    ]


def test_the_report_shows_the_planted_deviation(client, june_and_planted_may):
    """Приёмка T030 на живых данных и настоящей странице."""
    employee, code, _ledger = june_and_planted_may["loud"]
    login_as(client, "director")
    html = body(client.get(variance_url(client)))

    name = f"{employee.last_name} {employee.first_name}".strip()
    rows = report_rows(html)
    assert (name, code, PLANTED) in rows, (
        f"подброшенного отклонения {name} / {code} на +{PLANTED} в отчёте нет:\n{rows}"
    )


def test_the_report_keeps_quiet_about_the_change_within_the_threshold(
    client, june_and_planted_may
):
    """Иначе отчёт показывает всё подряд, и находка тонет в шуме."""
    employee, code, _ledger = june_and_planted_may["quiet"]
    login_as(client, "director")
    rows = report_rows(body(client.get(variance_url(client))))

    name = f"{employee.last_name} {employee.first_name}".strip()
    assert (name, code) not in {(row[0], row[1]) for row in rows}


def test_the_accountant_is_never_told_about_other_ledgers(client, june_and_planted_may):
    """D023 на новой поверхности: отклонение по чужому регистру — та же утечка."""
    login_as(client, "accountant")
    html = body(client.get(variance_url(client)))
    for name in ("Дополнительный", "Внутренний"):
        assert name not in html, f"в отчёте бухгалтера есть слово «{name}»"


def test_the_report_of_each_role_matches_what_that_role_sees(client, june_and_planted_may):
    """Итоги роли равны сумме её же строк — не маскировка на выводе.

    Итогов два, рост и снижение врозь (T096), и сходиться обязаны оба: один
    показанный итог не поймал бы перепутанные местами половины.
    """
    for user in ("director", "accountant", "manager"):
        login_as(client, user)
        html = body(client.get(variance_url(client)))
        rows = report_rows(html)
        found = re.findall(r'<td class="num total">([^<]+)</td>', html)
        assert len(found) == 2, f"{user}: в отчёте не два итога, а {len(found)}"
        up, down = (Decimal(v.replace(" ", "").replace(",", ".")) for v in found)

        assert sum((row[2] for row in rows if row[2] > 0), Decimal(0)) == up, (
            f"{user}: выросшие строки дают одно, итог роста другое"
        )
        assert sum((row[2] for row in rows if row[2] < 0), Decimal(0)) == down, (
            f"{user}: снизившиеся строки дают одно, итог снижения другое"
        )


def test_a_period_without_a_previous_one_says_so(client, web_env):
    """«Сравнивать не с чем» — ответ, а не пустая таблица без объяснения."""
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200

    from core.models import PayComponent, Payrun, Payslip, Tenant

    tenant = Tenant.objects.get(code="rs-dev")
    may = Payrun.objects.filter(tenant=tenant, period=MAY).first()
    if may is not None:
        PayComponent.objects.filter(payslip__payrun=may).delete()
        Payslip.objects.filter(payrun=may).delete()

    html = body(client.get(variance_url(client)))
    assert "сравнивать" in html.lower()
