"""Заведённому с экрана человеку есть куда внести часы (issue #152).

Что было. Сотрудника завести из интерфейса можно (D049, T164), а часы ему —
нет: строку табеля создавала **только загрузка таблицы партнёра**. Человек,
вышедший на работу в середине месяца, попадал в справочник и пропадал из
табеля: в сетке его не было, вписать часы было некуда, и в ведомость он не
приходил вовсе. Владелец так и спросил 27.08.2026: «проверь чтобы была
возможность ручного внесения людей, часов. сейчас я так понимаю что только
через табель можно внести».

Что теперь. Сетка показывает **всех, у кого действуют условия найма в этом
месяце**, а не только тех, у кого уже есть строка. Строка заводится в момент
первой правки — с нормой месяца из производственного календаря и пометкой
источника `manual`, чтобы её было видно отдельно от загруженных.

Почему строка не создаётся при открытии страницы. Открытие сетки — чтение;
запись на GET означала бы, что зашедший посмотреть директор оставляет после
себя тридцать пустых строк, а закрытие месяца видит их как данные.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as
from test_timesheets import grid_url  # noqa: F401

JUNE = date(2026, 6, 1)
NAME = "Пробников"


@pytest.fixture
def hired_from_screen(web_env):
    """Человек, заведённый как это делает экран: карточка плюс условия найма."""
    from core.models import Employee, EmployeeGroup, EmploymentTerm, Tenant, Timesheet, Unit

    tenant = Tenant.objects.get(code="rs-dev")
    group = EmployeeGroup.objects.filter(tenant=tenant).order_by("code").first()
    unit = Unit.objects.filter(tenant=tenant).order_by("code").first()
    person = Employee.objects.create(
        tenant=tenant, external_id="SCREEN-0001",
        first_name="Марко", last_name=NAME, hired_at=date(2026, 6, 4),
    )
    EmploymentTerm.objects.create(
        tenant=tenant, employee=person, group=group, unit=unit,
        base_rate=Decimal("420.0000"), coefficient=Decimal("1.0000"),
        valid_from=date(2026, 6, 4),
    )
    yield person
    Timesheet.objects.filter(employee=person).delete()
    EmploymentTerm.objects.filter(employee=person).delete()
    Employee.objects.filter(pk=person.pk).delete()


def june_grid(client) -> str:
    """Адрес сетки открытого месяца сида."""
    from core.models import Period

    period = Period.objects.get(period=JUNE)
    return f"/timesheets/{period.id}/"


def test_the_person_appears_in_the_grid_without_any_import(client, hired_from_screen):
    """Главное: человек виден в табеле, хотя таблицу партнёра никто не грузил."""
    login_as(client, "director")
    html = body(client.get(june_grid(client)))
    assert NAME in html, "заведённого с экрана человека нет в табеле — часы вписать некуда"


def test_opening_the_grid_creates_nothing(client, hired_from_screen):
    """Чтение остаётся чтением: строки заводятся правкой, а не просмотром."""
    from core.models import Timesheet

    login_as(client, "director")
    client.get(june_grid(client))
    assert not Timesheet.objects.filter(employee=hired_from_screen).exists(), (
        "открытие страницы завело строку — закрытие месяца увидит её как данные"
    )


def test_the_first_hours_create_the_row(client, hired_from_screen):
    """Правка ячейки заводит строку и сохраняет часы."""
    from core.models import Timesheet

    login_as(client, "director")
    url = june_grid(client)
    response = client.post(url + "cell/", {
        "employee": str(hired_from_screen.id), "kind": "regular", "hours": "12",
    })
    assert response.status_code == 200, body(response)[:400]

    row = Timesheet.objects.filter(employee=hired_from_screen).first()
    assert row is not None, "часы приняты, а строки нет"
    assert Decimal(str(row.hours.get("regular"))) == Decimal("12")
    assert row.source == "manual", "строку, заведённую с экрана, не отличить от загруженной"
    assert row.norm_hours > 0, "норма месяца не проставлена — расчёт не с чем сравнивать"


def test_the_hours_survive_a_reload(client, hired_from_screen):
    """Введённое видно на странице после перезагрузки, а не только в ответе."""
    login_as(client, "director")
    url = june_grid(client)
    client.post(url + "cell/", {
        "employee": str(hired_from_screen.id), "kind": "regular", "hours": "8",
    })
    html = body(client.get(url))
    assert NAME in html
    assert "8" in html


def test_a_second_edit_does_not_create_a_second_row(client, hired_from_screen):
    """Строка заводится один раз: ограничение базы иначе отвергнет вторую."""
    from core.models import Timesheet

    login_as(client, "director")
    url = june_grid(client)
    for hours in ("8", "10"):
        client.post(url + "cell/", {
            "employee": str(hired_from_screen.id), "kind": "regular", "hours": hours,
        })
    rows = Timesheet.objects.filter(employee=hired_from_screen)
    assert rows.count() == 1
    assert Decimal(str(rows.first().hours.get("regular"))) == Decimal("10")


def test_a_manager_of_another_unit_cannot_create_the_row(client, hired_from_screen):
    """Чужая точка — отказ, и по ответу не видно, что такой человек есть.

    Проверка стоит здесь, а не только на существующих строках: новый путь
    записи обязан спрашивать право так же, как старый, иначе разграничение
    обходится заведением человека.
    """
    from core.models import Timesheet

    login_as(client, "manager")
    response = client.post(june_grid(client) + "cell/", {
        "employee": str(hired_from_screen.id), "kind": "regular", "hours": "8",
    })
    assert response.status_code in (403, 404), body(response)[:300]
    assert not Timesheet.objects.filter(employee=hired_from_screen).exists()
