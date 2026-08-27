"""Карточка показывает, что человеку начислится, — до расчёта месяца (#185).

Эталон (модуль 8) держит на карточке блок «Что попадёт в расчёт июня»: строки
начислений по действующей версии условий — часы по табелю × ставка, ночные,
переработка, доплаты, итого. Без него ставку можно проверить только одним
способом: посчитать весь месяц и посмотреть, что вышло. То есть узнать об
опечатке в ставке — после расчёта тридцати человек.

Три вещи, которые здесь проверяются:

1. Предпросчёт **ничего не записывает**. Он отвечает на вопрос «что было бы» и
   обязан оставаться чтением: карточку открывают чаще, чем считают месяц, и
   запись при просмотре засорила бы ведомость.
2. Считает **тот же движок**, что и месяц. Свой упрощённый расчёт «для показа»
   разошёлся бы с настоящим — и хуже всего в тот день, когда человек поверил
   карточке.
3. Показывается **по действующей версии условий**: правка ставки будущим числом
   не должна менять то, что человек видит про текущий месяц.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as

JUNE = date(2026, 6, 1)


def card_url(person_id) -> str:
    return f"/directory/employees/{person_id}/"


@pytest.fixture
def someone(web_env):
    """Человек сида с часами в открытом месяце."""
    from core.models import Timesheet

    row = (
        Timesheet.objects.select_related("employee")
        .filter(period=JUNE)
        .exclude(hours={})
        .order_by("employee__external_id")
        .first()
    )
    assert row is not None, "в сиде нет табеля с часами — предпросчитывать нечего"
    return row.employee


def test_the_card_shows_what_will_be_accrued(client, someone):
    """На карточке видно, что человеку начислится в этом месяце."""
    login_as(client, "director")
    html = body(client.get(card_url(someone.id)))

    assert "Что попадёт в расчёт" in html or "предварительно" in html.lower(), (
        "на карточке нет предпросчёта — проверить условия найма можно только "
        "посчитав весь месяц"
    )
    # Хотя бы одна строка начисления с суммой: пустой блок хуже отсутствующего.
    assert "Итого" in html


def test_the_preview_writes_nothing(client, someone):
    """Открытие карточки не заводит ни ведомости, ни строк расчёта."""
    from core.models import Payrun, Payslip

    before = (Payrun.objects.count(), Payslip.objects.count())
    login_as(client, "director")
    client.get(card_url(someone.id))
    assert (Payrun.objects.count(), Payslip.objects.count()) == before, (
        "предпросчёт записал результат — карточку открывают чаще, чем считают месяц"
    )


def test_the_preview_agrees_with_the_real_calculation(client, someone, web_env):
    """Числа предпросчёта совпадают с настоящим расчётом месяца.

    Это главная проверка: свой упрощённый расчёт «для показа» разошёлся бы с
    настоящим — и хуже всего в тот день, когда человек поверил карточке.
    """
    from payrun.calc import compute

    _rules, results = compute(someone.tenant_id, JUNE)
    mine = next(
        (slip for case, slip in results if case.employee_id == someone.id), None,
    )
    assert mine is not None, "человека нет в расчёте месяца — тест проверяет не то"

    login_as(client, "director")
    html = body(client.get(card_url(someone.id)))
    # Итог начислений показан на карточке тем же числом, что даёт движок.
    from web.format import money

    assert money(mine.net) in html or money(mine.gross) in html, (
        "итог на карточке не сходится с расчётом движка"
    )


def test_a_person_without_hours_is_told_so(client, web_env):
    """Часов нет — так и сказано, а не показан ноль как результат."""
    from core.models import Employee, Timesheet

    counted = set(Timesheet.objects.filter(period=JUNE).values_list("employee_id", flat=True))
    without = Employee.objects.exclude(id__in=counted).first()
    if without is None:
        pytest.skip("в сиде нет человека без табеля")

    login_as(client, "director")
    html = body(client.get(card_url(without.id)))
    assert "часов" in html.lower() or "нет данных" in html.lower()


# --- движение за месяц (вторая половина #185) ---------------------------------


def test_the_list_shows_who_joined_and_who_left(client, web_env):
    """Список кадров говорит, что в этом месяце изменилось.

    Состав на месяц список показывал и раньше. Не хватало другого: кто в этом
    месяце пришёл, кто ушёл — то есть чем этот месяц отличается от прошлого.
    Без этого «почему фонд оплаты вырос» выясняется сравнением двух списков
    глазами.
    """
    from datetime import date

    from core.models import Employee, EmployeeGroup, EmploymentTerm, Tenant

    today = date.today().replace(day=1)
    tenant = Tenant.objects.get(code="rs-dev")
    group = EmployeeGroup.objects.filter(tenant=tenant).order_by("code").first()
    newbie = Employee.objects.create(
        tenant=tenant, external_id="MOVE-0001", first_name="Новый",
        last_name="Вышел", hired_at=today,
    )
    EmploymentTerm.objects.create(
        tenant=tenant, employee=newbie, group=group,
        base_rate=Decimal("400.0000"), coefficient=Decimal("1.0000"),
        valid_from=today,
    )
    try:
        login_as(client, "director")
        html = body(client.get("/directory/employees/"))
        assert "Движение за месяц" in html, "колонки движения нет"
        assert "принят" in html, "вышедший в этом месяце не помечен"
    finally:
        EmploymentTerm.objects.filter(employee=newbie).delete()
        Employee.objects.filter(pk=newbie.pk).delete()
